#pragma once

#include "../Cache/GameCache_s.hpp"
#include "../Console/Logger.hpp"
#include "../Game/State/GameState_s.hpp"

#include <filesystem>
#include <memory>
#include <string>

// Logs the bank's contents whenever a bank is opened: the account's own JSON line, a latest-snapshot
// file, and one compact log line. Deliberately open-only: deposits and withdrawals while the bank stays
// open aren't tracked, which keeps it a single cache lookup per open.
class BankLogger
{
public:
    BankLogger(const GameCache_s& cache, std::shared_ptr<Logger> logger, std::string accountName, std::string world, std::filesystem::path directory);

    // Called every step while in game. Logs on the closed -> open edge only.
    void Step(const GameState_s& state);

    [[nodiscard]] const std::filesystem::path& GetDirectory() const { return m_directory; }

private:
    void Log(const GameState_s& state);
    [[nodiscard]] bool IsBankOpen(const GameState_s& state) const;

    const GameCache_s& m_cache;
    std::shared_ptr<Logger> m_logger;
    std::string m_accountName;
    std::string m_world;
    std::filesystem::path m_directory;
    bool m_wasOpen = false;
};
