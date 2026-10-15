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
    // installable | installed | update | queued | installing | removing | blocked
    std::string state;
    std::string reason; // why it's blocked
    float progress = 0.0f;
    bool has_cover = false;
};

std::vector<Game> parse_library(std::string_view text);

// What removing a game would delete, as the PC previews it (console_uninstall_preview
// in backend/app/main.py): one tab-separated line.
struct Removal
{
    std::string game_id;
    std::string id; // the PC's preview id; empty when it can't be removed (error says why)
    std::string path;
    std::uint64_t bytes = 0; // estimated space freed; 0: unknown
    std::string warnings;
    std::string error;
};

bool parse_removal(std::string_view text, Removal *out);

// How a removal this app started ended (console_removal in backend/app/main.py).
struct RemovalResult
{
    std::string id;
    bool ok = false;
    bool tile_left = false; // its home-screen tile must still be deleted by hand
    std::string message;
};

// False while it's still running.
bool parse_removal_result(std::string_view text, RemovalResult *out);

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
