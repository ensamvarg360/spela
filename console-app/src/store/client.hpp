// PS5 Local Storefront - Talks to the PC app on a background thread.
// SPDX-License-Identifier: GPL-3.0-or-later
//
// The screen never waits on the network: it asks for things here and picks
// up the answers on a later frame.

#pragma once

#include "store/library.hpp"

#include <pthread.h>

#include <atomic>
#include <cstdint>
#include <deque>
#include <string>
#include <vector>

namespace hui::gfx
{
class GlBatch;
}

namespace hui::store
{

// Covers are uploaded as textures, which only the frame loop can do; main.cpp
// hands over the renderer's batch once it exists. Null in GL-free tests.
void set_texture_batch(gfx::GlBatch *batch);
gfx::GlBatch *texture_batch();

constexpr int kCoverWidth = 256; // the PC sends covers as raw RGBA of this size
constexpr int kCoverHeight = 320;

class Client
{
  public:
    Client();
    ~Client();
    Client(const Client &) = delete;
    Client &operator=(const Client &) = delete;

    // False when assets/server.txt is missing or unreadable (not paired).
    bool paired() const
    {
        return paired_;
    }
    const Server &server() const
    {
        return server_;
    }

    // ---- frame loop side ----
    // Copies the newest library into *games when it changed since the last
    // call. *error is empty when the last request worked.
    bool take_library(std::vector<Game> *games, std::string *error);
    void refresh();
    void install(const std::string &game_id);
    // The answer to an install: "Queued." or why not.
    bool take_message(std::string *message, bool *ok);
    void request_cover(const std::string &game_id);
    bool take_cover(std::string *game_id, std::vector<std::uint8_t> *rgba);

  private:
    struct Job
    {
        enum Kind
        {
            cover,
            install
        } kind;
        std::string game_id;
    };

    static void *thread_main(void *self);
    void run();
    bool step(); // one request, if one is due; false when idle
    void drain();
    void fetch_library();

    Server server_;
    bool paired_ = false;
    pthread_t thread_{};
    bool started_ = false;
    std::atomic<bool> stop_{false};
    std::atomic<bool> refresh_{true};
    std::int64_t next_poll_ = 0; // worker side only

    pthread_mutex_t lock_ = PTHREAD_MUTEX_INITIALIZER;
    // everything below is guarded by lock_
    std::vector<Game> games_;
    std::string error_;
    bool library_new_ = false;
    bool busy_ = false; // something installing: poll faster
    std::deque<Job> jobs_;
    std::deque<std::pair<std::string, std::vector<std::uint8_t>>> covers_;
    std::deque<std::pair<std::string, bool>> messages_;
};

} // namespace hui::store
