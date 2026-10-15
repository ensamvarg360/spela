// PS5 Local Storefront - Talks to the PC app on a background thread.
// SPDX-License-Identifier: GPL-3.0-or-later

#include "store/client.hpp"

#include "core/save_file.hpp"
#include "platform/ps5/system.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <cstdlib>
#include <cstring>

namespace hui::store
{

namespace
{

gfx::GlBatch *g_batch = nullptr;

// /app0 is the app's own folder, /data/homebrew/<TITLE_ID> as the PC sees it.
// A PC preview points HUI_SERVER_FILE somewhere else.
std::string server_file()
{
    const char *path = std::getenv("HUI_SERVER_FILE");
    return path ? path : "/app0/assets/server.txt";
}

constexpr std::int64_t kSecond = 1000000;
constexpr std::int64_t kPollIdle = 10 * kSecond;
constexpr std::int64_t kPollBusy = 2 * kSecond; // while something installs

struct Response
{
    int status = 0; // 0: no answer
    std::string body;
};

// One HTTP/1.0 request; the PC closes the connection after answering, so the
// body is everything after the headers. The host is an IPv4 address (the PC
// writes it), so there is no name lookup.
// ponytail: blocking connect; a PC that's off makes the first poll wait for the
// system's connect timeout. It's on the worker thread, so the screen keeps going.
Response request(const Server &server, const char *method, const std::string &path,
                 std::size_t limit)
{
    Response response;
    sockaddr_in address{};
#if !defined(__linux__)
    address.sin_len = sizeof(address);
#endif
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<std::uint16_t>(server.port));
    if (inet_pton(AF_INET, server.host.c_str(), &address.sin_addr) != 1)
        return response;
    const int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
        return response;
    timeval timeout{5, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    int flags = 0;
#ifdef SO_NOSIGPIPE
    int one = 1;
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
#ifdef MSG_NOSIGNAL
    flags = MSG_NOSIGNAL;
#endif
    if (connect(fd, reinterpret_cast<const sockaddr *>(&address), sizeof(address)) != 0)
    {
        close(fd);
        return response;
    }
    const std::string head = std::string(method) + " " + path + " HTTP/1.0\r\nHost: " +
                             server.host + "\r\nAuthorization: Bearer " + server.token +
                             "\r\nContent-Length: 0\r\n\r\n";
    for (std::size_t sent = 0; sent < head.size();)
    {
        const ssize_t n = send(fd, head.data() + sent, head.size() - sent, flags);
        if (n <= 0)
        {
            close(fd);
            return response;
        }
        sent += static_cast<std::size_t>(n);
    }
    std::string data;
    char buffer[16384];
    for (;;)
    {
        const ssize_t n = recv(fd, buffer, sizeof(buffer), 0);
        if (n < 0)
        {
            close(fd);
            return response; // timed out or dropped: a cut-off answer is no answer
        }
        if (n == 0)
            break;
        data.append(buffer, static_cast<std::size_t>(n));
        if (data.size() > limit + 4096)
            break;
    }
    close(fd);
    const std::size_t body = data.find("\r\n\r\n");
    if (data.compare(0, 5, "HTTP/") != 0 || body == std::string::npos)
        return response;
    response.status = std::atoi(data.c_str() + data.find(' ') + 1);
    response.body = data.substr(body + 4);
    return response;
}

std::string unpaired_message()
{
    return "This app isn't paired with your PC any more. On the PC, open Installed and "
           "click Update PS5 app.";
}

} // namespace

void set_texture_batch(gfx::GlBatch *batch)
{
    g_batch = batch;
}

gfx::GlBatch *texture_batch()
{
    return g_batch;
}

Client::Client()
{
    std::string text;
    paired_ = save::read_file(server_file(), &text, 4096) && parse_server(text, &server_);
    // A PC preview renders faster than real time, so it does the work inline (see drain()).
    if (!paired_ || std::getenv("HUI_SERVER_FILE"))
        return;
    started_ = pthread_create(&thread_, nullptr, &Client::thread_main, this) == 0;
    if (!started_)
        sys::log("[STORE] network thread creation failed");
}

void Client::drain()
{
    if (paired_ && !started_)
        while (step())
        {
        }
}

Client::~Client()
{
    stop_ = true;
    if (started_)
        pthread_join(thread_, nullptr);
}

void *Client::thread_main(void *self)
{
    static_cast<Client *>(self)->run();
    return nullptr;
}

void Client::run()
{
    while (!stop_)
        if (!step())
            sys::sleep_us(50000);
}

bool Client::step()
{
    pthread_mutex_lock(&lock_);
    const bool have_job = !jobs_.empty();
    Job job{Job::cover, {}};
    if (have_job)
    {
        job = jobs_.front();
        jobs_.pop_front();
    }
    const bool busy = busy_;
    pthread_mutex_unlock(&lock_);

    if (have_job && job.kind == Job::install)
    {
        const Response r = request(server_, "POST", "/api/v1/console-app/install/" + job.game_id, 4096);
        const std::string text = r.status == 0     ? "Can't reach the PC."
                                 : r.status == 401 ? unpaired_message()
                                                   : r.body;
        pthread_mutex_lock(&lock_);
        messages_.emplace_back(text, r.status == 200);
        pthread_mutex_unlock(&lock_);
        refresh_ = true; // show it as queued straight away
        return true;
    }
    if (have_job)
    {
        const std::size_t size = static_cast<std::size_t>(kCoverWidth) * kCoverHeight * 4;
        Response r = request(server_, "GET", "/api/v1/console-app/covers/" + job.game_id, size);
        if (r.status == 200 && r.body.size() == size)
        {
            pthread_mutex_lock(&lock_);
            covers_.emplace_back(job.game_id, std::vector<std::uint8_t>(r.body.begin(), r.body.end()));
            pthread_mutex_unlock(&lock_);
        }
        return true;
    }
    if (refresh_.exchange(false) || sys::monotonic_us() >= next_poll_)
    {
        fetch_library();
        next_poll_ = sys::monotonic_us() + (busy ? kPollBusy : kPollIdle);
        return true;
    }
    return false;
}

void Client::fetch_library()
{
    const Response r = request(server_, "GET", "/api/v1/console-app/library", 8u << 20);
    pthread_mutex_lock(&lock_);
    if (r.status == 200)
    {
        games_ = parse_library(r.body);
        error_.clear();
        busy_ = false;
        for (const Game &g : games_)
            busy_ = busy_ || g.state == "queued" || g.state == "installing";
    }
    else if (r.status == 401)
    {
        error_ = unpaired_message();
    }
    else if (r.status == 0)
    {
        error_ = "Can't reach the PC at " + server_.url + ". Is PS5 Local Storefront running there, "
                 "with \"Allow other devices on my network\" ticked?";
    }
    else
    {
        error_ = "The PC answered with an error (" + std::to_string(r.status) + ").";
    }
    library_new_ = true;
    pthread_mutex_unlock(&lock_);
}

bool Client::take_library(std::vector<Game> *games, std::string *error)
{
    drain();
    pthread_mutex_lock(&lock_);
    const bool fresh = library_new_;
    if (fresh)
    {
        if (error_.empty())
            *games = games_; // on an error the last good list stays up
        *error = error_;
        library_new_ = false;
    }
    pthread_mutex_unlock(&lock_);
    return fresh;
}

void Client::refresh()
{
    refresh_ = true;
}

void Client::install(const std::string &game_id)
{
    pthread_mutex_lock(&lock_);
    jobs_.push_front({Job::install, game_id}); // ahead of any covers still loading
    pthread_mutex_unlock(&lock_);
}

bool Client::take_message(std::string *message, bool *ok)
{
    drain();
    pthread_mutex_lock(&lock_);
    const bool have = !messages_.empty();
    if (have)
    {
        *message = messages_.front().first;
        *ok = messages_.front().second;
        messages_.pop_front();
    }
    pthread_mutex_unlock(&lock_);
    return have;
}

void Client::request_cover(const std::string &game_id)
{
    pthread_mutex_lock(&lock_);
    jobs_.push_back({Job::cover, game_id});
    pthread_mutex_unlock(&lock_);
}

bool Client::take_cover(std::string *game_id, std::vector<std::uint8_t> *rgba)
{
    drain();
    pthread_mutex_lock(&lock_);
    const bool have = !covers_.empty();
    if (have)
    {
        *game_id = std::move(covers_.front().first);
        *rgba = std::move(covers_.front().second);
        covers_.pop_front();
    }
    pthread_mutex_unlock(&lock_);
    return have;
}

} // namespace hui::store
