#include "pch.hpp"
#include "StatusWriter.hpp"

#include "../Script/Host/ScriptHost.hpp"
#include "Account.hpp"

namespace
{
    constexpr auto STATUS_EXTENSION = ".json";

    std::string Escape(std::string_view text)
    {
        auto out = std::string{};
        out.reserve(text.size() + 2);
        for (const auto character : text)
        {
            switch (character)
            {
            case '"':
                out += "\\\"";
                break;
            case '\\':
                out += "\\\\";
                break;
            case '\n':
                out += "\\n";
                break;
            case '\t':
                out += "\\t";
                break;
            default:
                if (static_cast<unsigned char>(character) < 0x20)
                {
                    std::format_to(std::back_inserter(out), "\\u{:04x}", static_cast<unsigned>(character));
                }
                else
                {
                    out += character;
                }
                break;
            }
        }

        return out;
    }

    std::string_view ClientStateName(ClientStatus_e status)
    {
        switch (status)
        {
        case ClientStatus_e::Disconnected:
            return "disconnected";
        case ClientStatus_e::Connecting:
            return "connecting";
        case ClientStatus_e::InGame:
            return "ingame";
        case ClientStatus_e::LoggingOut:
            return "logging out";
        case ClientStatus_e::LoggedOut:
            return "logged out";
        }

        return "unknown";
    }

    // The shortened names the progress files use, in stat order.
    constexpr auto STAT_NAMES = std::to_array<std::string_view>({
        "attack", "defence", "strength", "hitpoints", "ranged", "prayer", "magic",
        "cooking", "woodcutting", "fletching", "fishing", "firemaking", "crafting",
        "smithing", "mining", "herblore", "agility", "thieving", "runecraft",
    });
}

StatusWriter::StatusWriter(std::filesystem::path file, std::string world)
    : m_path{std::move(file)}
    , m_world{std::move(world)}
{
}

void StatusWriter::Step(Clock::time_point now, const std::vector<std::unique_ptr<Account>>& accounts, bool lifeChanged)
{
    const auto intervalDue = !m_next || now >= *m_next;
    if (!lifeChanged && !intervalDue)
    {
        return;
    }

    if (intervalDue)
    {
        m_next = now + INTERVAL;
    }

    auto json = std::string{};
    json.reserve(accounts.size() * 512 + 128);
    // period: the 5-minute write. event: a lifecycle write (login, logout, script change), which the
    // GUI shows at once but excludes from its rates.
    const auto* const kind = intervalDue ? "period" : "event";
    std::format_to(std::back_inserter(json), "{{\n  \"world\": \"{}\",\n  \"writtenAt\": \"{}\",\n  \"kind\": \"{}\",\n  \"accounts\": [\n",
                   Escape(m_world), std::format("{:%Y-%m-%dT%H:%M:%SZ}", std::chrono::floor<std::chrono::seconds>(std::chrono::system_clock::now())),
                   kind);

    auto signature = std::string{};
    auto first = true;
    for (const auto& accountPtr : accounts)
    {
        const auto& account = *accountPtr;
        const auto& client = account.GetClient();
        const auto& state = client.GetState();
        const auto script = account.GetScriptFile();

        if (!first)
        {
            json += ",\n";
        }
        first = false;

        const auto status = client.GetStatus();
        const auto inGame = status == ClientStatus_e::InGame && state.placed;
        const auto tile = inGame ? state.localPlayer.tile : Tile_s{};

        std::format_to(std::back_inserter(json),
                       "    {{\n      \"name\": \"{}\",\n      \"username\": \"{}\",\n      \"script\": \"{}\",\n"
                       "      \"state\": \"{}\",\n      \"status\": \"{}\",\n      \"placed\": {},\n"
                       "      \"loginCount\": {},\n      \"tile\": {{\"x\": {}, \"z\": {}, \"level\": {}}},\n"
                       "      \"hp\": {},\n      \"maxHp\": {},\n      \"run\": {},\n      \"skills\": {{",
                       Escape(account.GetName()), Escape(account.GetUsername()), Escape(script),
                       ClientStateName(status), Escape(account.DescribeStatus()), inGame,
                       client.GetLoginCount(),
                       tile.x, tile.z, tile.level,
                       state.localPlayer.hits.empty() ? 0 : static_cast<int>(state.localPlayer.hits.back().health),
                       state.localPlayer.hits.empty() ? 0 : static_cast<int>(state.localPlayer.hits.back().maxHealth),
                       static_cast<int>(state.runEnergy));

        std::format_to(std::back_inserter(signature), "{}:{}:{}:{}|", account.GetName(), ClientStateName(status), script, state.tick);

        for (std::size_t i = 0; i < STAT_NAMES.size() && i < GameState_s::STAT_COUNT; ++i)
        {
            const auto& stat = state.stats[i];
            std::format_to(std::back_inserter(json), "{}\"{}\": {{\"level\": {}, \"base\": {}, \"xp\": {}}}", i == 0 ? "" : ", ", STAT_NAMES[i], stat.level, stat.baseLevel, stat.xp);
        }

        json += "}\n    }";
    }

    json += "\n  ]\n}\n";

    // Nothing changed since the last write and the interval hasn't come round again: skip the I/O.
    // A first write (no signature yet) always goes out, so a status file exists before the first change.
    if (!lifeChanged && !m_lastSignature.empty() && signature == m_lastSignature)
    {
        return;
    }
    m_lastSignature = std::move(signature);

    try
    {
        const auto folder = m_path.parent_path();
        if (!folder.empty())
        {
            auto error = std::error_code{};
            std::filesystem::create_directories(folder, error);
        }

        auto temporary = m_path;
        temporary += ".tmp";
        auto file = std::ofstream{temporary, std::ios::binary | std::ios::trunc};
        file << json;
        file.flush();
        if (!file)
        {
            throw std::runtime_error{std::format("can't write {}", temporary.string())};
        }
        file.close();

        std::error_code error;
        std::filesystem::rename(temporary, m_path, error);
        if (error)
        {
            std::filesystem::remove(temporary, error);
        }
    }
    catch (const std::exception&)
    {
        // Status reporting is a convenience: never let it take the client down.
    }
}
