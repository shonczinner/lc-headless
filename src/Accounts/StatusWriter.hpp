#pragma once

#include <chrono>
#include <filesystem>
#include <optional>
#include <string>
#include <vector>

class Account;

// Writes every account's skill levels and state to one JSON file, for the GUI to read.
// Independent of scripts and progress reports, so it works with any bot, and cheap enough to
// run on a slow schedule.
class StatusWriter
{
public:
    using Clock = std::chrono::steady_clock;

    static constexpr auto INTERVAL = std::chrono::minutes{5};

    StatusWriter(std::filesystem::path file, std::string world);

    // Writes when the interval has passed, or always when forced (a login, a script starting or
    // stopping, a logout), so the GUI is never blind to lifecycle changes.
    void Step(Clock::time_point now, const std::vector<std::unique_ptr<Account>>& accounts, bool force = false);

    [[nodiscard]] const std::filesystem::path& GetPath() const { return m_path; }

private:
    std::filesystem::path m_path;
    std::string m_world;
    std::optional<Clock::time_point> m_next;
    std::string m_lastSignature;
};
