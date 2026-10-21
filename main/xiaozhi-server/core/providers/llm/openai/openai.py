import httpx
import openai
from openai.types import CompletionUsage
from config.logger import setup_logging
from core.utils.util import check_model_key
from core.providers.llm.base import LLMProviderBase
from urllib.parse import urlparse
from core.providers.llm.request_limits import request_lock, fit_messages, context_overflow
from core.providers.llm.cancellation import SessionRequests

TAG = __name__
logger = setup_logging()

# 需要禁用思考模式的平台域名及其对应参数（默认关闭思考模式）
THINKING_DISABLED_DOMAINS = {
    "aliyuncs.com": {"enable_thinking": False},
    "deepseek.com": {"thinking": {"type": "disabled"}},
    "bigmodel.cn": {"thinking": {"type": "disabled"}},
    "moonshot.cn": {"thinking": {"type": "disabled"}},
    "volces.com": {"thinking": {"type": "disabled"}},
}


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.model_name = config.get("model_name")
        self._sessions = SessionRequests()
        self.api_key = config.get("api_key")
        if "base_url" in config:
            self.base_url = config.get("base_url")
        else:
            self.base_url = config.get("url")
        url = urlparse(self.base_url)
        local_studio = url.hostname in {"localhost", "127.0.0.1", "host.docker.internal", "::1"} and url.port == 1234
        self.context_window = int(config.get("context_window") or (8192 if local_studio else 0))
        self.serialize_requests = config.get("serialize_requests", local_studio)
        self.reasoning_effort = config.get("reasoning_effort",
            "none" if local_studio and "qwen" in (self.model_name or "").lower() else None)
        
        timeout_config = config.get("timeout")
        if isinstance(timeout_config, dict):
            # 细粒度超时配置
            custom_timeout = httpx.Timeout(
                pool=timeout_config.get("pool", 2.0),
                connect=timeout_config.get("connect", 3.0),
                write=timeout_config.get("write", 5.0),
                read=timeout_config.get("read", 60.0)
            )
        elif isinstance(timeout_config, (int, float)) and timeout_config > 0:
            # 兼容旧的单一超时配置（整数或浮点数）
            custom_timeout = httpx.Timeout(timeout_config)
        else:
            # 未配置或配置无效，使用默认值
            custom_timeout = httpx.Timeout(300)

        param_defaults = {
            "max_tokens": int,
            "temperature": lambda x: round(float(x), 1),
            "top_p": lambda x: round(float(x), 1),
            "frequency_penalty": lambda x: round(float(x), 1),
        }

        for param, converter in param_defaults.items():
            value = config.get(param)
            try:
                setattr(
                    self,
                    param,
                    converter(value) if value not in (None, "") else None,
                )
            except (ValueError, TypeError):
                setattr(self, param, None)

        logger.debug(
            f"意图识别参数初始化: {self.temperature}, {self.max_tokens}, {self.top_p}, {self.frequency_penalty}"
        )

        model_key_msg = check_model_key("LLM", self.api_key)
        if model_key_msg:
            logger.bind(tag=TAG).error(model_key_msg)
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=custom_timeout)

    @staticmethod
    def normalize_dialogue(dialogue):
        """自动修复 dialogue 中缺失 content 的消息"""
        dialogue = [dict(msg) for msg in dialogue]
        for msg in dialogue:
            if "role" in msg and "content" not in msg:
                msg["content"] = ""
        return dialogue

    def _apply_thinking_disabled(self, request_params: dict):
        """根据域名自动禁用思考模式"""
        if self.reasoning_effort is not None:
            # LM Studio uses this field for Qwen; chat_template_kwargs does not
            # disable its reasoning. Hidden reasoning otherwise exhausts the
            # short JSON/extraction output budget before returning any content.
            request_params["reasoning_effort"] = self.reasoning_effort
        parsed_url = urlparse(self.base_url)
        domain = parsed_url.netloc
        for disabled_domain, params in THINKING_DISABLED_DOMAINS.items():
            if disabled_domain in domain:
                request_params.setdefault("extra_body", {}).update(params)
                logger.bind(tag=TAG).info(f"为域名 {domain} 禁用思考模式，参数: {params}")
                break

    def cancel_response(self, session_id):
        self._sessions.cancel(session_id)

    def _stream_request(self, request_params, session_id=""):
        ticket = self._sessions.register(session_id)
        try:
            yield from self._stream_cancellable(request_params, ticket)
        finally:
            self._sessions.finish(session_id, ticket)

    def _stream_cancellable(self, request_params, ticket):
        # LM Studio's parallel slots share the loaded model's KV cache. Memory
        # extraction and speech chat must not fill it simultaneously.
        with request_lock(self.base_url, self.model_name, self.serialize_requests):
            if ticket.cancelled.is_set():
                return
            original = request_params["messages"]
            output = request_params.get("max_tokens") or 1024
            if self.context_window:
                output = min(int(output), max(128, self.context_window // 4))
                budget = self.context_window - output - 512
            else:
                budget = None
            for attempt in range(2):
                if ticket.cancelled.is_set():
                    return
                params = dict(request_params)
                params["messages"] = fit_messages(original, params.get("tools"), budget)
                if self.context_window:
                    params["max_tokens"] = output
                responses = None
                delivered = False
                try:
                    responses = self.client.chat.completions.create(**params)
                    ticket.attach(responses)
                    for chunk in responses:
                        if ticket.cancelled.is_set():
                            return
                        choices = getattr(chunk, "choices", None)
                        delta = choices[0].delta if choices else None
                        if delta and (getattr(delta, "content", None) or getattr(delta, "tool_calls", None)):
                            delivered = True
                        yield chunk
                    return
                except Exception as error:
                    if ticket.cancelled.is_set():
                        return
                    if attempt or delivered or budget is None or not context_overflow(error):
                        raise
                    logger.bind(tag=TAG).warning("模型上下文不足，缩减本次历史和输出预算后重试；本地原文不变")
                    budget = max(256, int(budget * 0.65))
                    output = max(128, output // 2)
                finally:
                    if responses is not None:
                        responses.close()

    def response(self, session_id, dialogue, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
        }

        # 添加可选参数,只有当参数不为None时才添加
        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
            "response_format": kwargs.get("response_format"),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)

        responses = self._stream_request(request_params, session_id)

        is_active = True
        try:            
            for chunk in responses:
                try:
                    delta = chunk.choices[0].delta if getattr(chunk, "choices", None) else None
                    content = getattr(delta, "content", "") if delta else ""
                except IndexError:
                    content = ""
                if content:
                    if "<think>" in content:
                        is_active = False
                        content = content.split("<think>")[0]
                    if "</think>" in content:
                        is_active = True
                        content = content.split("</think>")[-1]
                    if is_active:
                        yield content
        finally:
            responses.close()

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        dialogue = self.normalize_dialogue(dialogue)

        request_params = {
            "model": self.model_name,
            "messages": dialogue,
            "stream": True,
            "tools": functions,
        }

        optional_params = {
            "max_tokens": kwargs.get("max_tokens", self.max_tokens),
            "temperature": kwargs.get("temperature", self.temperature),
            "top_p": kwargs.get("top_p", self.top_p),
            "frequency_penalty": kwargs.get("frequency_penalty", self.frequency_penalty),
        }

        for key, value in optional_params.items():
            if value is not None:
                request_params[key] = value

        # 禁用思考模式
        self._apply_thinking_disabled(request_params)

        stream = self._stream_request(request_params, session_id)

        try:
            for chunk in stream:
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", "")
                    tool_calls = getattr(delta, "tool_calls", None)
                    yield content, tool_calls
                elif isinstance(getattr(chunk, "usage", None), CompletionUsage):
                    usage_info = getattr(chunk, "usage", None)
                    logger.bind(tag=TAG).info(
                        f"Token 消耗：输入 {getattr(usage_info, 'prompt_tokens', '未知')}，"
                        f"输出 {getattr(usage_info, 'completion_tokens', '未知')}，"
                        f"共计 {getattr(usage_info, 'total_tokens', '未知')}"
                    )
        finally:
            stream.close()
