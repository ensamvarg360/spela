// PS5 Local Storefront - The PC app's library as the console app sees it.
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The PC sends plain text (backend/app/main.py, console_library): one game per
// line, tab-separated, so the app needs no JSON parser.

#pragma once

#include <cstdint>
#include <string>
#include <string_view>
#include <vector>

namespace hui::store
{

struct Game
{
    std::string id;
    std::string title;
    std::string title_id;
    std::string version;
    std::uint64_t size_bytes = 0;
    // installable | installed | update | queued | installing | blocked
    std::string state;
    std::string reason; // why it's blocked
    float progress = 0.0f;
    bool has_cover = false;
};

std::vector<Game> parse_library(std::string_view text);

// Where the PC app is: assets/server.txt, written by the PC when it installs
// this app ("url=http://192.168.0.40:8787" and "token=..." lines).
struct Server
{
    std::string url; // as written, for messages
    std::string host;
    int port = 0;
    std::string token;
};

bool parse_server(std::string_view text, Server *out);

// "40.2 GB", decimal units like the PC app.
std::string format_size(std::uint64_t bytes);

} // namespace hui::store
