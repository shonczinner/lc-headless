#include "pch.hpp"
#include "AccountRunner.hpp"

#include "../Cache/GameCache_s.hpp"
#include "../Config/AccountConfig_s.hpp"
#include "../Config/Config_s.hpp"
#include "../Console/ConsoleCommandParser.hpp"
#include "../Console/ConsoleCommand_s.hpp"
#include "../Console/ConsoleInput.hpp"
#include "../Console/Logger.hpp"
#include "../Game/Net/LoginQueue.hpp"
#include "../Io/NetLoop.hpp"
#include "../Script/BotMessenger.hpp"
#include "../Script/ScriptRuntime.hpp"
#include "../Utils/StringUtils.hpp"
#include "Account.hpp"
#include "AccountCommands.hpp"

namespace
{
    constexpr auto TARGET_PREFIX = ConsoleCommandParser::TARGET_PREFIX;
    // A little over AsioLink's close timeout, so a close the server never answers still finishes.
    constexpr auto SETTLE_TIMEOUT = 2s;

}

AccountRunner::AccountRunner(std::shared_ptr<const Config_s> config, std::string configPath, std::shared_ptr<const GameCache_s> cache, std::vector<AccountConfig_s> accounts, ScriptRuntime& runtime, std::shared_ptr<Logger> logger, AccountOptions_s options)
    : m_config{std::move(config)}
    , m_configPath{std::move(configPath)}
    , m_cache{std::move(cache)}
    , m_logger{std::move(logger)}
    , m_loginQueue{std::make_shared<LoginQueue>(m_config->scripting.loginIntervalSeconds)}
    , m_commands{m_accounts, *m_logger}
{
    assert(m_cache && "AccountRunner needs a cache");

    // Status for the GUI, named after the client config: client_w1.jsonc -> status/client_w1.json.
    if (!m_config->scripting.statusDirectory.empty())
    {
        auto stem = std::filesystem::path{m_configPath}.stem().string();
        if (stem.empty())
        {
            stem = "client";
        }
        m_status.emplace(std::filesystem::path{m_config->scripting.statusDirectory} / (stem + ".json"), m_config->server.host);
    }

    options.client.loginQueue = m_loginQueue;
    for (auto& account : accounts)
    {
        m_accounts.push_back(std::make_unique<Account>(m_config, m_cache, std::move(account), runtime, m_logger, options, &m_messenger, m_net));
    }
}

bool AccountRunner::Run(const InterruptCount& interrupts, ConsoleInput* console)
{
    m_loginTurns = 0;
    m_nextLogin = m_accounts.empty() ? std::nullopt : std::optional{Clock::now()};

    auto handledInterrupts = interrupts();

    m_logger->Info("Running {} account{}", m_accounts.size(), m_accounts.size() == 1 ? "" : "s");

    if (console != nullptr)
    {
        console->SetOnArrived([net = m_net]
        {
            net->Wake();
        });
    }

    while (true)
    {
        // Before the steps, so a command queued for a script runs in this pass.
        if (console != nullptr)
        {
            for (const auto& line : console->TakeLines())
            {
                HandleCommand(line);
            }
        }

        const auto now = Clock::now();
        if (m_nextLogin && now >= *m_nextLogin)
        {
            StartNextLogin(now);
        }

        for (const auto& account : m_accounts)
        {
            account->Step(0ms);
        }

        for (const auto count = interrupts(); handledInterrupts < count; ++handledInterrupts)
        {
            InterruptAll();
        }

        const auto running = std::ranges::any_of(m_accounts, [](const std::unique_ptr<Account>& account)
        {
            return account->IsStarted() && !account->IsFinished();
        });

        if (!running && !m_nextLogin)
        {
            break;
        }

        WriteStatus();

        Wait(now);
    }

    if (console != nullptr)
    {
        console->SetOnArrived({});
    }

    // So every account's last close ends with a close frame rather than a dropped socket.
    m_net->Settle(Clock::now() + SETTLE_TIMEOUT);
    return LogOutcomes();
}

void AccountRunner::HandleCommand(std::string_view line)
{
    const auto parsed = ConsoleCommandParser::Parse(line);
    if (!parsed)
    {
        return;
    }

    Account* target = nullptr;

    if (const auto& name = parsed->target)
    {
        target = m_commands.FindAccount(*name);
        if (target == nullptr)
        {
            auto names = std::vector<std::string>{};
            for (const auto& account : m_accounts)
            {
                names.push_back(account->GetName());
            }

            m_logger->Warning("No account is named {}; the accounts are {}", *name, StringUtils::Join(names, ", "));
            return;
        }

        if (parsed->command.name.empty())
        {
            m_logger->Warning("{}{} needs a command after it, such as {}{} {}", TARGET_PREFIX, *name, TARGET_PREFIX, *name, AccountCommands::HELP);
            return;
        }
    }

    const auto& command = parsed->command;
    if (command.name == AccountCommands::HELP)
    {
        m_commands.ShowHelp(target);
    }
    else if (command.name == AccountCommands::STATUS)
    {
        m_commands.ShowStatus(target);
    }
    else if (command.name == AccountCommands::QUIT || command.name == AccountCommands::EXIT)
    {
        Quit(target);
    }
    else if (command.name == AccountCommands::LOG_LEVEL)
    {
        m_commands.SetLogLevel(target, command.args);
    }
    else if (command.name == AccountCommands::SAY)
    {
        m_commands.Say(target, command.text);
    }
    else
    {
        m_commands.SendToScripts(target, command);
    }
}

std::size_t AccountRunner::GetAccountCount() const
{
    return m_accounts.size();
}

const Account& AccountRunner::GetAccount(std::size_t index) const
{
    assert(index < m_accounts.size() && "Account index out of range");
    return *m_accounts[index];
}

// Accounts quit from the console before their turn give it up to the next.
void AccountRunner::StartNextLogin(Clock::time_point now)
{
    while (m_loginTurns < m_accounts.size() && m_cancelled.contains(m_accounts[m_loginTurns].get()))
    {
        ++m_loginTurns;
    }

    if (m_loginTurns < m_accounts.size())
    {
        m_accounts[m_loginTurns]->Start();
        ++m_loginTurns;
    }

    const auto waiting = m_loginTurns < m_accounts.size();
    m_nextLogin = waiting ? std::optional{now + m_config->scripting.loginIntervalSeconds} : std::nullopt;
}

void AccountRunner::InterruptAll()
{
    if (m_nextLogin)
    {
        const auto waiting = m_accounts.size() - m_loginTurns;
        m_logger->Info("Interrupted; {} account{} won't log in", waiting, waiting == 1 ? "" : "s");
        m_nextLogin.reset();
    }

    for (const auto& account : m_accounts)
    {
        account->Interrupt();
    }
}

void AccountRunner::Quit(Account* target)
{
    if (target == nullptr)
    {
        InterruptAll();
        return;
    }

    if (target->IsStarted())
    {
        target->Interrupt();
        return;
    }

    if (m_cancelled.insert(target).second)
    {
        m_logger->Info("{} won't log in", target->GetName());
    }
}

void AccountRunner::Wait(Clock::time_point now) const
{
    auto wake = now + m_config->scripting.pollIntervalMs;
    if (m_nextLogin)
    {
        wake = std::min(wake, *m_nextLogin);
    }

    for (const auto& account : m_accounts)
    {
        if (const auto nextLoop = account->GetNextLoop())
        {
            wake = std::min(wake, *nextLoop);
        }
    }

    // Wakes early for any account's I/O, and for a console line through SetOnArrived.
    m_net->Wait(wake);
}

void AccountRunner::WriteStatus()
{
    if (!m_status)
    {
        return;
    }

    // A login, a script change or a logout should show up at once, not on the next interval.
    auto signature = std::string{};
    for (const auto& account : m_accounts)
    {
        const auto& client = account->GetClient();
        std::format_to(std::back_inserter(signature), "{}:{}:{}|", account->GetName(), static_cast<int>(client.GetStatus()), account->GetScriptFile());
    }

    // The periodic write feeds the GUI's xp/hr; a lifecycle change just brings the snapshot forward.
    // Why: a force flag would mark every login burst as "unscheduled", so rates would never compute.
    const auto lifeChanged = signature != m_statusSignature;
    m_statusSignature = std::move(signature);
    m_status->Step(Clock::now(), m_accounts, lifeChanged);
}

bool AccountRunner::LogOutcomes() const
{
    auto succeeded = true;
    for (const auto& account : m_accounts)
    {
        if (!account->IsStarted())
        {
            continue;
        }

        succeeded = succeeded && account->Succeeded();
        m_logger->Info("{}: {}", account->GetName(), account->DescribeOutcome());
    }

    return succeeded;
}
