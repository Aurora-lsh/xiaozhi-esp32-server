#include "main/boards/otto-robot/subtitle_pages.h"
#include "main/protocols/protocol.h"
#include <algorithm>
#include <cassert>
#include <iostream>
#include <memory>

int Measure(const std::string &text) {
  int width = 0;
  for (size_t i = 0; i < text.size();) {
    auto c = static_cast<unsigned char>(text[i]);
    size_t length = c < 0x80 ? 1 : c < 0xE0 ? 2 : c < 0xF0 ? 3 : 4;
    width += length == 1 ? 10 : 20;
    i += length;
  }
  return width;
}

void CheckPages(const std::string &text) {
  const auto pages = MakeSubtitlePages(text, 224, Measure);
  std::string restored;
  for (const auto &page : pages) {
    assert(std::count(page.begin(), page.end(), '\n') <= 1);
    size_t begin = 0;
    do {
      auto end = page.find('\n', begin);
      auto line =
          page.substr(begin, end == std::string::npos ? end : end - begin);
      assert(Measure(line) <= 224);
      restored += line;
      if (end == std::string::npos)
        break;
      begin = end + 1;
    } while (true);
  }
  auto original = text;
  original.erase(std::remove(original.begin(), original.end(), '\n'),
                 original.end());
  original.erase(std::remove(original.begin(), original.end(), '\r'),
                 original.end());
  assert(original == restored);
}

int main() {
  CheckPages("");
  CheckPages("short");
  CheckPages(std::string(100, 'x'));
  CheckPages(
      u8"海绵宝宝和派大星带着橙色纸风车回到菠萝屋，准备明天再去水母田玩。");
  CheckPages(u8"Hello海绵宝宝！\n比奇堡🍍\r\n继续讲故事。");
  CheckPages(std::string(1000, 'a') + u8"海绵宝宝");
  using Status = Protocol::ResponseStatus;
  for (const auto &input :
       {"{}", "{\"state\":null}", "{\"state\":3}", "{\"state\":\"bad\"}"}) {
    auto *json = cJSON_Parse(input);
    assert(Protocol::ParseResponseStatus(json) == Status::Unknown);
    cJSON_Delete(json);
  }
  auto *thinking = cJSON_Parse("{\"state\":\"thinking\"}");
  auto *speaking = cJSON_Parse("{\"state\":\"speaking\"}");
  assert(Protocol::ParseResponseStatus(thinking) == Status::Thinking);
  assert(Protocol::ParseResponseStatus(speaking) == Status::Speaking);
  cJSON_Delete(thinking);
  cJSON_Delete(speaking);
  std::cout << "6 subtitle cases and 6 protocol cases passed\n";
}
