#include "pch.hpp"
#include "BankLogger.hpp"

#include "../Console/Logger.hpp"

namespace
{
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

    std::string Now()
    {
        return std::format("{:%Y-%m-%dT%H:%M:%SZ}", std::chrono::floor<std::chrono::seconds>(std::chrono::system_clock::now()));
    }

    void WriteFile(const std::filesystem::path& path, std::string_view text, bool append)
    {
        auto error = std::error_code{};
        if (!path.parent_path().empty())
        {
            std::filesystem::create_directories(path.parent_path(), error);
        }

        auto file = std::ofstream{path, std::ios::binary | (append ? std::ios::app : std::ios::trunc)};
        file << text;
        file.flush();
        if (!file)
        {
            throw std::runtime_error{std::format("can't write {}", path.string())};
        }
    }
}

BankLogger::BankLogger(const GameCache_s& cache, std::shared_ptr<Logger> logger, std::string accountName, std::string world, std::filesystem::path directory)
    : m_cache{cache}
    , m_logger{std::move(logger)}
    , m_accountName{std::move(accountName)}
    , m_world{std::move(world)}
    , m_directory{std::move(directory)}
{
}

bool BankLogger::IsBankOpen(const GameState_s& state) const
{
    if (!state.placed || m_cache.bankComponent == 0)
    {
        return false;
    }

    const auto* const bank = m_cache.FindComponent(static_cast<s32>(m_cache.bankComponent));
    return bank != nullptr && bank->root != 0 && state.interfaces.mainModal == static_cast<s32>(bank->root);
}

void BankLogger::Step(const GameState_s& state)
{
    const auto open = IsBankOpen(state);
    if (open && !m_wasOpen)
    {
        Log(state);
    }

    m_wasOpen = open;
}

void BankLogger::Log(const GameState_s& state)
{
    const auto* const inventory = state.FindInventory(m_cache.bankComponent);
    if (inventory == nullptr)
    {
        // Opened but the item list hasn't arrived; the next open will catch it.
        return;
    }

    auto items = std::string{};
    auto summary = std::string{};
    for (const auto& slot : inventory->slots)
    {
        if (slot.id < 0 || slot.count <= 0)
        {
            continue;
        }

        const auto name = m_cache.FindObjName(slot.id);
        if (!items.empty())
        {
            items += ", ";
            summary += ", ";
        }

        std::format_to(std::back_inserter(items), "{{\"id\": {}, \"name\": \"{}\", \"qty\": {}}}", slot.id, Escape(name), slot.count);
        std::format_to(std::back_inserter(summary), "{} x{}", name.empty() ? std::format("#{}", slot.id) : std::string{name}, slot.count);
    }

    const auto time = Now();
    auto record = std::string{};
    std::format_to(std::back_inserter(record), "{{\"time\": \"{}\", \"account\": \"{}\", \"world\": \"{}\", \"items\": [{}]}}\n",
                   time, Escape(m_accountName), Escape(m_world), items);

    try
    {
        const auto base = m_directory / m_accountName;
        WriteFile(base.string() + ".jsonl", record, true);
        WriteFile(base.string() + ".json",
                  std::format("{{\n  \"time\": \"{}\",\n  \"account\": \"{}\",\n  \"world\": \"{}\",\n  \"items\": [{}]\n}}\n", time, Escape(m_accountName), Escape(m_world), items),
                  false);
    }
    catch (const std::exception&)
    {
        // Bank logging is a convenience: never let it take the account down.
    }

    m_logger->Info("Bank (open): {}", summary.empty() ? "empty" : summary);
}
