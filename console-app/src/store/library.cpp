// Spela - Parsing the PC's library and server files.
// SPDX-License-Identifier: GPL-3.0-or-later

#include "store/library.hpp"

#include <cstdio>
#include <cstdlib>

namespace hui::store
{

namespace
{

std::vector<std::string_view> split(std::string_view text, char separator)
{
    std::vector<std::string_view> parts;
    for (;;)
    {
        const std::size_t at = text.find(separator);
        parts.push_back(text.substr(0, at));
        if (at == std::string_view::npos)
            return parts;
        text.remove_prefix(at + 1);
    }
}

std::string_view trim(std::string_view s)
{
    while (!s.empty() && (s.back() == '\r' || s.back() == ' '))
        s.remove_suffix(1);
    while (!s.empty() && s.front() == ' ')
        s.remove_prefix(1);
    return s;
}

} // namespace

std::vector<ConsoleGame> parse_console_games(std::string_view text)
{
    std::vector<ConsoleGame> games;
    for (std::string_view line : split(text, '\n'))
    {
        const std::vector<std::string_view> f = split(trim(line), '\t');
        if (f.size() < 5 || f[0].empty())
            continue;
        ConsoleGame g{std::string(f[0]), std::string(f[1]), std::string(f[2]), f[3] == "1", f[4] == "1"};
        if (f.size() >= 7)
        {
            g.kind = f[5];
            g.version = f[6];
        }
        games.push_back(std::move(g));
    }
    return games;
}

bool parse_pack(std::string_view text, Pack *out)
{
    const std::vector<std::string_view> f = split(trim(split(text, '\n')[0]), '\t');
    if (f.size() < 9)
        return false;
    out->running = f[0] == "1";
    out->kind = f[1];
    out->phase = f[2];
    out->done = std::strtoull(std::string(f[3]).c_str(), nullptr, 10);
    out->total = std::strtoull(std::string(f[4]).c_str(), nullptr, 10);
    out->before = std::strtoull(std::string(f[5]).c_str(), nullptr, 10);
    out->after = std::strtoull(std::string(f[6]).c_str(), nullptr, 10);
    out->game = f[7];
    out->error = f[8];
    return true;
}

std::vector<Game> parse_library(std::string_view text)
{
    std::vector<Game> games;
    for (std::string_view line : split(text, '\n'))
    {
        const std::vector<std::string_view> f = split(trim(line), '\t');
        if (f.size() < 8 || f[0].empty())
            continue; // a blank last line, or a format this build doesn't know
        Game g;
        g.id = f[0];
        g.title = f[1];
        g.title_id = f[2];
        g.version = f[3];
        g.size_bytes = std::strtoull(std::string(f[4]).c_str(), nullptr, 10);
        const std::string_view state = f[5];
        if (state.substr(0, 8) == "blocked:")
        {
            g.state = "blocked";
            g.reason = state.substr(8);
        }
        else
        {
            g.state = state;
        }
        g.progress = f[6].empty() ? 0.0f : std::strtof(std::string(f[6]).c_str(), nullptr);
        g.has_cover = f[7] == "1";
        if (f.size() >= 10)
        {
            g.kind = f[8];
            g.format = f[9];
        }
        if (f.size() >= 15)
        {
            g.bytes_done = std::strtoull(std::string(f[10]).c_str(), nullptr, 10);
            g.bytes_total = std::strtoull(std::string(f[11]).c_str(), nullptr, 10);
            g.rate_bps = std::strtoull(std::string(f[12]).c_str(), nullptr, 10);
            g.seconds_left = f[13].empty() ? -1 : std::atoi(std::string(f[13]).c_str());
            g.step = f[14];
        }
        if (f.size() >= 17)
        {
            g.favorite = f[15] == "1";
            g.added = std::strtoll(std::string(f[16]).c_str(), nullptr, 10);
        }
        if (f.size() >= 18)
            for (std::string_view genre : split(f[17], '|'))
                if (!genre.empty())
                    g.genres.emplace_back(genre);
        if (f.size() >= 19)
            g.has_logo = f[18] == "1";
        games.push_back(std::move(g));
    }
    return games;
}

std::vector<Drive> parse_drives(std::string_view text)
{
    std::vector<Drive> drives;
    for (std::string_view line : split(text, '\n'))
    {
        const std::vector<std::string_view> f = split(trim(line), '\t');
        if (f.size() < 4 || f[0].empty())
            continue;
        drives.push_back({std::string(f[0]), std::string(f[1]),
                          std::strtoull(std::string(f[2]).c_str(), nullptr, 10),
                          std::strtoull(std::string(f[3]).c_str(), nullptr, 10)});
    }
    return drives;
}

bool parse_removal(std::string_view text, Removal *out)
{
    const std::vector<std::string_view> f = split(trim(split(text, '\n')[0]), '\t');
    if (f.size() < 4 || f[0].empty())
        return false;
    out->id = f[0];
    out->path = f[1];
    out->bytes = std::strtoull(std::string(f[2]).c_str(), nullptr, 10);
    out->warnings = f[3];
    return true;
}

bool parse_removal_result(std::string_view text, RemovalResult *out)
{
    const std::vector<std::string_view> f = split(trim(split(text, '\n')[0]), '\t');
    if (f.size() < 3 || f[0] == "removing")
        return false;
    out->ok = f[0] == "success";
    out->tile_left = f[1] == "1";
    out->message = f[2];
    return true;
}

bool parse_server(std::string_view text, Server *out)
{
    Server s;
    for (std::string_view line : split(text, '\n'))
    {
        line = trim(line);
        if (line.substr(0, 4) == "url=")
            s.url = line.substr(4);
        else if (line.substr(0, 6) == "token=")
            s.token = line.substr(6);
    }
    std::string_view rest = s.url;
    if (rest.substr(0, 7) != "http://")
        return false;
    rest.remove_prefix(7);
    const std::size_t colon = rest.find(':');
    if (colon == std::string_view::npos)
        return false;
    s.host = rest.substr(0, colon);
    s.port = std::atoi(std::string(rest.substr(colon + 1)).c_str());
    if (s.host.empty() || s.port <= 0 || s.token.empty())
        return false;
    *out = std::move(s);
    return true;
}

std::string format_size(std::uint64_t bytes)
{
    static const char *const kUnits[] = {"B", "KB", "MB", "GB", "TB"};
    double n = static_cast<double>(bytes);
    int unit = 0;
    while (n >= 1000.0 && unit < 4)
    {
        n /= 1000.0;
        ++unit;
    }
    char text[32];
    std::snprintf(text, sizeof(text), unit ? "%.1f %s" : "%.0f %s", n, kUnits[unit]);
    return text;
}

std::string format_duration(int seconds)
{
    char text[32];
    if (seconds < 60)
        return "Less than a minute left";
    if (seconds < 3600)
        std::snprintf(text, sizeof(text), "About %d min left", (seconds + 30) / 60);
    else
        std::snprintf(text, sizeof(text), "About %d h %d min left", seconds / 3600, seconds % 3600 / 60);
    return text;
}

} // namespace hui::store
