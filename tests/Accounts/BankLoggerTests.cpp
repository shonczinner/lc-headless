#include "pch.hpp"
#include "../Support/TempFolder.hpp"

#include "Accounts/BankLogger.hpp"
#include "Cache/GameCache_s.hpp"
#include "Console/LogEntry_s.hpp"
#include "Console/LogLevel_e.hpp"
#include "Console/Logger.hpp"
#include "Game/State/GameState_s.hpp"

#include <catch2/catch_test_macros.hpp>

namespace
{
    constexpr auto BANK_ROOT = u16{5292};
    constexpr auto BANK_ITEM_LIST = u16{5382};
    constexpr auto COINS = u16{995};
    constexpr auto BONES = u16{526};

    std::string ReadFile(const std::filesystem::path& path)
    {
        auto file = std::ifstream{path, std::ios::binary};
        return std::string{std::istreambuf_iterator<char>{file}, std::istreambuf_iterator<char>{}};
    }

    // The bank the cache needs, and the two items the tests put in it.
    std::shared_ptr<const GameCache_s> MakeBankCache()
    {
        auto cache = GameCache_s{};
        cache.bankComponent = BANK_ITEM_LIST;

        cache.components[BANK_ROOT] = IfComponent_s{.id = BANK_ROOT, .root = BANK_ROOT, .type = ComponentType_e::Layer};
        cache.components[BANK_ITEM_LIST] = IfComponent_s{.id = BANK_ITEM_LIST, .root = BANK_ROOT, .type = ComponentType_e::Inv};

        // FindObj indexes objs by id, so every earlier id needs a slot.
        cache.objs.resize(static_cast<std::size_t>(COINS) + 1);
        cache.objs[COINS] = ObjType_s{.id = COINS, .name = "Coins"};
        cache.objs[BONES] = ObjType_s{.id = BONES, .name = "Bones"};
        return std::make_shared<const GameCache_s>(std::move(cache));
    }

    GameState_s OpenBank(std::vector<Item_s> items)
    {
        auto state = GameState_s{};
        state.placed = true;
        state.interfaces.mainModal = BANK_ROOT;
        state.inventories[BANK_ITEM_LIST] = Inventory_s{.com = BANK_ITEM_LIST, .slots = std::move(items)};
        return state;
    }

    std::shared_ptr<Logger> SilentLogger()
    {
        return std::make_shared<Logger>(LogLevel_e::Error, [](const LogEntry_s&) {});
    }
}

TEST_CASE("BankLogger writes one snapshot per bank open", "[BankLogger]")
{
    const auto cache = MakeBankCache();
    const auto folder = TempFolder{"rs2004-bank-logger-tests"};
    auto logger = BankLogger{*cache, SilentLogger(), "bot1", "w1.example", folder.GetPath()};

    SECTION("nothing is written before a bank opens")
    {
        auto state = GameState_s{};
        state.placed = true;
        logger.Step(state);
        CHECK_FALSE(std::filesystem::exists(folder.GetPath() / "bot1.json"));
    }

    SECTION("the bank's items are written with their names, ids and counts")
    {
        logger.Step(OpenBank({{.id = COINS, .count = 1234}, {.id = BONES, .count = 5}, {.id = -1, .count = 0}}));

        const auto latest = ReadFile(folder.GetPath() / "bot1.json");
        CHECK(latest.find(R"("account": "bot1")") != std::string::npos);
        CHECK(latest.find(R"("world": "w1.example")") != std::string::npos);
        CHECK(latest.find(R"({"id": 995, "name": "Coins", "qty": 1234})") != std::string::npos);
        CHECK(latest.find(R"({"id": 526, "name": "Bones", "qty": 5})") != std::string::npos);
        // Empty slots are left out.
        CHECK(latest.find(R"("id": -1)") == std::string::npos);

        CHECK(ReadFile(folder.GetPath() / "bot1.jsonl").find(R"("name": "Coins")") != std::string::npos);
    }

    SECTION("staying open logs once, and a reopen logs again")
    {
        logger.Step(OpenBank({{.id = COINS, .count = 10}}));
        const auto afterFirst = ReadFile(folder.GetPath() / "bot1.jsonl");
        logger.Step(OpenBank({{.id = COINS, .count = 10}}));
        CHECK(ReadFile(folder.GetPath() / "bot1.jsonl") == afterFirst);

        auto closed = GameState_s{};
        closed.placed = true;
        logger.Step(closed);
        logger.Step(OpenBank({{.id = COINS, .count = 20}}));

        CHECK(ReadFile(folder.GetPath() / "bot1.jsonl").size() > afterFirst.size());
        CHECK(ReadFile(folder.GetPath() / "bot1.json").find(R"("qty": 20)") != std::string::npos);
    }

    SECTION("an empty bank is logged as an empty list")
    {
        logger.Step(OpenBank({}));
        CHECK(ReadFile(folder.GetPath() / "bot1.json").find(R"("items": [])") != std::string::npos);
    }

    SECTION("the main modal has to be the bank's root")
    {
        auto state = OpenBank({{.id = COINS, .count = 3}});
        state.interfaces.mainModal = 1234;
        logger.Step(state);
        CHECK_FALSE(std::filesystem::exists(folder.GetPath() / "bot1.json"));
    }
}
