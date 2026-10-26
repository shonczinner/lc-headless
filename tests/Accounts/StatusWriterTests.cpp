#include "pch.hpp"
#include "../Support/Game/FakeGameServer.hpp"
#include "../Support/Game/TestWorld.hpp"
#include "../Support/LogCapture.hpp"
#include "../Support/Script/ScriptTestRuntime.hpp"
#include "../Support/TempFolder.hpp"

#include "Accounts/AccountRunner.hpp"
#include "Cache/GameCache_s.hpp"
#include "Config/AccountConfig_s.hpp"
#include "Config/Config_s.hpp"

#include <catch2/catch_test_macros.hpp>

namespace
{
    std::string ReadFile(const std::filesystem::path& path)
    {
        auto file = std::ifstream{path, std::ios::binary};
        return std::string{std::istreambuf_iterator<char>{file}, std::istreambuf_iterator<char>{}};
    }

    class StatusFixture
    {
    public:
        explicit StatusFixture(std::string logFile = "client_w1.log")
            : server{TestWorld::Send}
            , folder{"rs2004-status-tests"}
        {
            config = server.MakeConfig();
            config.scripting.statusDirectory = (folder.GetPath() / "status").string();
            config.client.logFile = std::move(logFile);
        }

        // The runner names the file after the config it was built from, so the fixture has to hand it the
        // config the test changed, not server.MakeConfig()'s copy.
        std::unique_ptr<AccountRunner> MakeRunnerFromConfig()
        {
            return std::make_unique<AccountRunner>(std::make_shared<const Config_s>(config), FakeGameServer::MakeCache(),
                                                   std::vector<AccountConfig_s>{}, ScriptTestRuntime::Get(), capture.GetLogger());
        }

        [[nodiscard]] std::filesystem::path StatusPath(std::string_view name) const
        {
            return folder.GetPath() / "status" / (std::string{name} + ".json");
        }

        FakeGameServer server;
        TempFolder folder;
        LogCapture capture;
        Config_s config;
    };
}

TEST_CASE("AccountRunner writes a status file for tools to read", "[AccountRunner][Status]")
{
    SECTION("named after the client's log file, with its host")
    {
        auto fixture = StatusFixture{"rs2004-headless-w2.log"};
        auto runner = fixture.MakeRunnerFromConfig();
        runner->WriteStatus();

        const auto path = fixture.StatusPath("rs2004-headless-w2");
        REQUIRE(std::filesystem::exists(path));
        const auto text = ReadFile(path);
        CHECK(text.find("\"world\"") != std::string::npos);
        CHECK(text.find("\"accounts\": [") != std::string::npos);
    }

    SECTION("falls back to client without a log file")
    {
        auto fixture = StatusFixture{""};
        auto runner = fixture.MakeRunnerFromConfig();
        runner->WriteStatus();
        CHECK(std::filesystem::exists(fixture.StatusPath("client")));
    }

    SECTION("statusName wins over the log file")
    {
        auto fixture = StatusFixture{"ignored.log"};
        fixture.config.scripting.statusName = "custom";
        auto runner = fixture.MakeRunnerFromConfig();
        runner->WriteStatus();
        CHECK(std::filesystem::exists(fixture.StatusPath("custom")));
    }

    SECTION("an empty statusDirectory writes nothing")
    {
        auto fixture = StatusFixture{};
        fixture.config.scripting.statusDirectory.clear();
        auto runner = fixture.MakeRunnerFromConfig();
        runner->WriteStatus();
        CHECK_FALSE(std::filesystem::exists(fixture.folder.GetPath() / "status"));
    }
}
