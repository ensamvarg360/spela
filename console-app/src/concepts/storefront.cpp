// Spela - The games on your PC: browse them, install and remove them.
// SPDX-License-Identifier: GPL-3.0-or-later
//
// A grid of covers on the left and the focused game on the right; L1 and R1
// switch between games, what's installed, DLC and the cart. Cross opens the install
// dialog (pick a drive, read any warning), and Cross again asks the PC to
// install it; the PC does the copying and this screen follows its progress.
// Options puts a game in the cart (a fourth tab), and Cross there installs
// everything in it, one after another, to one drive.
// R2 makes a game a favorite on the PC too; favorites come first in every tab
// but the cart. L2 sorts the rest by name, by when they were added, or by size.
// Up from the top row reaches a row of genres (PlayStation Store's, via the PC):
// left and right there show only that genre's games.
// The LZ4 tab lists the game folders on this PS5 itself, from the helper, so it
// works with the PC off: Cross packs one with LZ4 on the console, Square deletes
// the original it sets aside (or stops the packing), Options puts it back.
// Square on an install in progress offers to stop it. Square on an installed
// game asks the PC what removing it would delete, shows it, and Square again
// removes it; a dialog says how it ended, and whether its home-screen tile
// still has to be deleted by hand. All network traffic goes through
// store::Client on its own thread.

#include "concepts/concepts.hpp"
#include "concepts/logo.hpp"

#include "gfx/gl_batch.hpp"
#include "store/client.hpp"
#include "core/save_file.hpp"
#include "ui/glyphs.hpp"
#include "ui/motion.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace hui::concepts
{

namespace
{

using gfx::Color;
using gfx::Rect;

const Color kInk = Color::rgb(0xf2f4ff);
const Color kMuted = Color::rgb(0x9aa3c7);
const Color kPanel = Color::rgb(0x161a2b, 0.92f);
const Color kAccent = Color::rgb(0x4d8dff);
const Color kGood = Color::rgb(0x3ccf8e);
const Color kWarn = Color::rgb(0xffb347);
const Color kBad = Color::rgb(0xff6b6b);
const Color kTransparent = Color::rgb(0x000000, 0.0f);
const Color kWhite = Color::rgb(0xffffff);
const Color kNight = Color::rgb(0x06070f);
const Color kFavorite = Color::rgb(0xff5c8a);

constexpr float kMargin = 96.0f;
constexpr int kColumns = 5;
constexpr float kCardW = 200.0f;
constexpr float kCardH = kCardW * 320.0f / 256.0f; // the PC's cover shape
constexpr float kGap = 32.0f;
constexpr float kPitch = kCardH + 84.0f;
constexpr float kGenresY = 196.0f; // the row of genre chips between the tabs and the grid
constexpr float kChipH = 46.0f;
constexpr float kGridTop = 274.0f;
constexpr float kPanelTop = 170.0f;
constexpr float kGridBottom = 890.0f; // the hint bar below needs room
constexpr float kPanelX = 1368.0f;
constexpr float kPanelW = 1920.0f - kMargin - kPanelX;
constexpr float kMessageSeconds = 5.0f;
constexpr float kLift = 0.06f;  // how much the focused card grows
constexpr float kArtH = 260.0f; // the focused game's art at the top of the panel
constexpr int kMaxDrives = 5;   // rows in the install dialog
// LZ4 before the cart: L1 from Games still reaches the cart.
constexpr const char *kTabs[] = {"Games", "Installed", "DLC", "LZ4", "Cart"};
constexpr const char *kTabCounts[] = {"games", "installed", "DLC", "on this PS5", "in cart"};
constexpr int kTabCount = 5;
constexpr int kInstalledTab = 1;
constexpr int kLz4Tab = 3;
constexpr int kCartTab = 4;
constexpr float kRowH = 84.0f; // a game in the LZ4 tab's list
constexpr int kCheckoutNames = 5; // titles listed in the install-all dialog
constexpr const char *kSorts[] = {"Name", "Newest", "Size"};
constexpr int kSortCount = 3;
// Beside the kit's settings. The PC preview has no such folder, so it never saves.
constexpr const char *kSortFile = "/download0/hui/spela-sort.txt";

constexpr const char *kTechniques[] = {
    "Library from the PC app over HTTP, on a background thread",
    "Covers arrive as raw RGBA and become textures as they load",
    "L1 and R1 switch between games, installed games, DLC and the cart",
    "Favorites come first; L2 sorts the rest by name, newest or size",
    "A row of genre chips above the grid filters it, most common genre first",
    "The cart queues all its games at once; the PC copies them one after another",
    "Installs run on the PC, to the drive picked here; this screen polls their progress",
    "Removals show the PC's preview and wait for a second press",
    "The backdrop eases toward the focused cover's colours",
    "The LZ4 tab asks the helper on the console, not the PC, so it works with the PC off",
};

// Shown in the PC preview: the tour waits, moves, and installs one game.
constexpr app::TourStep kTour[] = {
    {1.0f, 0, Direction::none, "grid"},
    {0.3f, 0, Direction::right},
    {0.3f, 0, Direction::right},
    {0.8f, action_bit(Action::jump_next), Direction::none}, // a third favorite
    {0.8f, action_bit(Action::jump_prev), Direction::none, "favorite"},
    {0.8f, action_bit(Action::jump_prev), Direction::none, "sorted"}, // newest first
    {0.3f, action_bit(Action::jump_prev), Direction::none},           // back to by name
    {0.3f, action_bit(Action::jump_next), Direction::none},           // and the favorite undone
    {0.3f, 0, Direction::left},                                       // to the second favorite
    {0.8f, action_bit(Action::confirm), Direction::none, "focus"},
    {0.6f, action_bit(Action::confirm), Direction::none, "install"},
    {1.2f, 0, Direction::none, "queued"},
    {0.3f, 0, Direction::right},
    {0.3f, 0, Direction::right}, // to the installed one
    {0.8f, action_bit(Action::west), Direction::none},
    {0.8f, 0, Direction::none, "remove"},
    {0.2f, action_bit(Action::west), Direction::none},
    {0.6f, 0, Direction::none, "removing"},
    {0.2f, action_bit(Action::north), Direction::none}, // the preview outruns the poll timer
    {0.8f, action_bit(Action::confirm), Direction::none, "removed"},
    {0.3f, action_bit(Action::page_next), Direction::none},
    {0.3f, 0, Direction::right},              // past the favorite
    {0.8f, 0, Direction::right, "installed"}, // to the one installing
    {0.8f, 0, Direction::none, "progress"},
    {0.4f, action_bit(Action::west), Direction::none},
    {0.6f, action_bit(Action::west), Direction::none, "stop"},
    {0.3f, action_bit(Action::page_next), Direction::none},
    {0.8f, 0, Direction::none, "dlc"},
    {0.4f, action_bit(Action::menu), Direction::none}, // the DLC into the cart
    {0.3f, action_bit(Action::page_prev), Direction::none},
    {0.3f, action_bit(Action::page_prev), Direction::none},
    {0.6f, action_bit(Action::menu), Direction::none, "cart-add"}, // and the first game
    {0.3f, action_bit(Action::page_prev), Direction::none},
    {0.8f, 0, Direction::none, "cart"},
    {0.6f, action_bit(Action::confirm), Direction::none, "checkout"},
    {0.6f, action_bit(Action::confirm), Direction::none, "checked-out"},
    {0.3f, action_bit(Action::page_prev), Direction::none}, // back to Games
    {0.3f, 0, Direction::up},                               // to the genre row
    {0.4f, 0, Direction::right},
    {0.8f, 0, Direction::none, "genres"},
    {0.4f, 0, Direction::down},
    {0.8f, 0, Direction::none, "genre-grid"},
    {0.3f, action_bit(Action::page_prev), Direction::none}, // the cart
    {0.3f, action_bit(Action::page_prev), Direction::none}, // the LZ4 tab: the helper's list, not the PC's
    {0.8f, 0, Direction::none, "lz4"},
    {0.3f, 0, Direction::down}, // the packed one whose original is kept
    {0.8f, 0, Direction::none, "lz4-original"},
    {0.4f, action_bit(Action::west), Direction::none},
    {0.6f, 0, Direction::none, "lz4-delete"},
    {0.4f, action_bit(Action::back), Direction::none},
};

struct StateLook
{
    const char *label;
    Color color;
};

StateLook look(const store::Game &g)
{
    if (g.state == "installable")
        return {"On your PC", kAccent};
    if (g.state == "installed")
        return {"Installed", kGood};
    if (g.state == "update")
        return {"Newer version on PC", kWarn};
    if (g.state == "queued")
        return {"Waiting to install", kWarn};
    if (g.state == "installing")
        return {"Installing", kWarn};
    if (g.state == "removing")
        return {"Removing", kWarn};
    return {"Can't install", kBad};
}

// A cover's two main colours, top and bottom half: backdrop, glow, placeholder.
struct Tones
{
    Color top;
    Color bottom;
};

Color average(const std::vector<std::uint8_t> &rgba, int from_row, int to_row)
{
    float sum[3] = {};
    int n = 0;
    for (int y = from_row; y < to_row; y += 4)
        for (int x = 0; x < store::kCoverWidth; x += 4, ++n)
            for (int c = 0; c < 3; ++c)
                sum[c] += rgba[static_cast<std::size_t>((y * store::kCoverWidth + x) * 4 + c)];
    const float k = 1.0f / (255.0f * static_cast<float>(std::max(n, 1)));
    return {sum[0] * k, sum[1] * k, sum[2] * k, 1.0f};
}

// Games without a cover still get colours of their own, picked by title.
Tones placeholder_tones(const std::string &title)
{
    static const Tones kTones[] = {
        {Color::rgb(0x3b4fd8), Color::rgb(0x1a1446)}, {Color::rgb(0xc2416b), Color::rgb(0x2a0f2c)},
        {Color::rgb(0x1f9e8f), Color::rgb(0x0b2630)}, {Color::rgb(0xd9822b), Color::rgb(0x2d140c)},
        {Color::rgb(0x7b4ae2), Color::rgb(0x170f3a)}, {Color::rgb(0x2f8fd8), Color::rgb(0x0c1f3d)},
    };
    return kTones[std::hash<std::string>{}(title) % std::size(kTones)];
}

// A square stood on its corner with a disc on each upper edge, like the kit's Rating heart.
void draw_heart(gfx::DrawList &list, float cx, float cy, float size, Color color)
{
    const float a = size * 0.56f;
    const float y = cy + a * 0.0735f;
    const float off = a * 0.35355f;
    list.rotated_rect({cx - a * 0.5f, y - a * 0.5f, a, a}, a * 0.07f, 0.7853982f, color);
    list.circle(cx - off, y - off, a * 0.5f, color);
    list.circle(cx + off, y - off, a * 0.5f, color);
}

const char *pack_phase(const std::string &phase)
{
    return phase == "checking"    ? "Checking the game"
           : phase == "packing"   ? "Packing with LZ4"
           : phase == "copying"   ? "Copying the files that stay as they are"
           : phase == "verifying" ? "Checking every block"
           : phase == "waiting"   ? "Waiting for the game to be closed"
           : phase == "swapping"  ? "Putting it in place"
           : phase == "deleting"  ? "Deleting the original"
           : phase == "restoring" ? "Putting the original back"
                                  : "Working";
}

bool removable(const store::Game &g)
{
    return g.state == "installed" || g.state == "update";
}

bool installing(const store::Game &g)
{
    return g.state == "queued" || g.state == "installing";
}

bool fits_bytes(const store::Drive &d, std::uint64_t bytes)
{
    return d.total_bytes == 0 || d.free_bytes >= bytes; // unknown space: the PC checks
}

bool fits(const store::Drive &d, const store::Game &g)
{
    return fits_bytes(d, g.size_bytes);
}

// Games: all games. Installed: games on this console, or on their way in or out. DLC: all DLC.
// The cart tab lists the cart (Storefront::show_tab).
bool in_tab(const store::Game &g, int tab)
{
    if (tab == 2 || g.kind == "dlc")
        return tab == 2 && g.kind == "dlc";
    return tab == 0 || (g.state != "installable" && g.state != "blocked");
}

// What the LZ4 tab asks before it goes ahead.
enum class Lz4Ask
{
    none,
    pack,
    stop,
    keep,   // delete the original
    restore // put the original back
};

class Storefront final : public app::Concept
{
  public:
    explicit Storefront(app::Context &context) : context_(context)
    {
        focus_ring_.snap(card_rect(0).inset(-kCardW * kLift * 0.5f));
        const Color night[4] = {kNight, Color::rgb(0x0f1026), Color::rgb(0x1d2a6b), Color::rgb(0x3a1d5c)};
        for (int i = 0; i < 4; ++i)
            palette_[i].snap(night[i]);
        std::string saved;
        if (save::read_file(kSortFile, &saved, 16))
            sort_ = std::clamp(std::atoi(saved.c_str()), 0, kSortCount - 1);
    }

    const app::ConceptInfo &info() const override
    {
        static const app::ConceptInfo kInfo{
            "storefront",
            "Spela",
            "The games on your PC, ready to install",
            "console-app/src/concepts/storefront.cpp",
            audio::SoundSet::glass,
            kAccent,
            kTechniques,
        };
        return kInfo;
    }

    void enter() override
    {
        age_ = 0.0f;
    }

    void update(const InputFrame &input, float dt, app::Feedback &feedback) override
    {
        age_ += dt;
        clock_ += dt;
        message_age_ += dt;
        if (logo_ == 0 && store::texture_batch() != nullptr)
            logo_ = store::texture_batch()->create_texture(kLogoWidth, kLogoHeight, kLogoRgba);

        std::string error;
        const store::Game *was = focused();
        const std::string keep = was != nullptr ? was->id : std::string();
        if (client_.take_library(&games_, &drives_, &error))
        {
            if (!loaded_ && error.empty())
                age_ = 0.0f; // the cards' entrance starts when they arrive
            loaded_ = loaded_ || error.empty();
            error_ = error;
            // Whatever stopped being installable (queued from the PC, installed, gone) leaves the cart.
            std::erase_if(cart_, [this](const std::string &id) {
                const store::Game *g = find(id);
                return g == nullptr || g->state != "installable";
            });
            show_tab(keep);
        }
        std::string text;
        bool ok = false;
        while (client_.take_message(&text, &ok))
        {
            message_ = text;
            message_ok_ = ok;
            message_age_ = 0.0f;
            // A recorded chime: the glass set has no "complete", whose stand-in tone is a long rising sweep.
            feedback.play(ok ? audio::Cue::saved : audio::Cue::error);
        }
        if (client_.take_console(&console_games_, &pack_))
        {
            console_loaded_ = true;
            lz4_games_.clear();
            for (const store::ConsoleGame &g : console_games_)
                if (g.kind == "folder")
                    lz4_games_.push_back(g);
            lz4_focus_ = std::clamp(lz4_focus_, 0, std::max(0, static_cast<int>(lz4_games_.size()) - 1));
            if (offline()) // what's installed here, until the PC answers
            {
                games_ = offline_games();
                show_tab(keep);
            }
        }
        take_covers();
        store::Removal removal;
        while (client_.take_removal(&removal))
        {
            if (removal.game_id != awaiting_)
                continue; // asked for before the last one was answered
            awaiting_.clear();
            if (removal.id.empty())
            {
                message_ = removal.error;
                message_ok_ = false;
                message_age_ = 0.0f;
                feedback.play(audio::Cue::error);
            }
            else
            {
                confirm_ = removal;
                modal_.snap(0.0f);
                feedback.play(audio::Cue::modal_open);
            }
        }

        store::RemovalResult result;
        while (done_.id.empty() && client_.take_removal_result(&result))
        {
            done_ = result;
            modal_.snap(0.0f);
            done_title_ = removal_titles_[result.id];
            removal_titles_.erase(result.id);
            client_.refresh(); // the game's new state
            feedback.play(result.ok ? audio::Cue::notify : audio::Cue::error);
        }

        if (lz4_ask_ != Lz4Ask::none)
        {
            update_lz4_ask(input, feedback);
        }
        else if (!confirm_.id.empty())
        {
            update_confirm(input, feedback);
        }
        else if (!done_.id.empty())
        {
            if (input.is_pressed(Action::confirm) || input.is_pressed(Action::back))
            {
                done_ = {};
                feedback.play(audio::Cue::modal_close);
            }
        }
        else if (!install_id_.empty())
        {
            update_install(input, feedback);
        }
        else if (!stop_id_.empty())
        {
            update_stop(input, feedback);
        }
        else if (checkout_)
        {
            update_checkout(input, feedback);
        }
        else
        {
            const int before = focus_;
            update_grid(input, feedback, static_cast<int>(shown_.size()));
            if (focus_ != before)
                lift_.snap(0.0f);
        }

        request_covers();
        const Rect card = card_rect(focus_);
        scroll_.reveal(card.y - kGridTop, card.y - kGridTop + kPitch, kGridBottom - kGridTop, 24.0f);
        scroll_.update(dt);
        const Rect chip = chip_rect(genre_index());
        chips_scroll_.reveal(chip.x - kMargin, chip.x + chip.w - kMargin, kPanelX - kGap - kMargin, 48.0f);
        chips_scroll_.update(dt);
        focus_ring_.target(card.inset(-kCardW * kLift * 0.5f));
        focus_ring_.update(dt, 20.0f);
        refusal_.update(dt, 9.0f);
        lift_.target = 1.0f;
        lift_.update(dt, 14.0f);
        modal_.target = dialog_open() ? 1.0f : 0.0f;
        if (modal_.target == 0.0f)
            modal_.snap(0.0f); // what it showed is gone, so it can't fade out
        modal_.update(dt, 16.0f);

        // The backdrop takes the focused cover's colours, eased.
        if (const store::Game *g = focused())
        {
            const Tones t = tones(*g);
            const Color targets[4] = {gfx::mix(t.bottom, kNight, 0.7f), gfx::mix(t.top, kNight, 0.55f),
                                      gfx::mix(t.top, kNight, 0.1f), gfx::mix(t.bottom, kWhite, 0.1f)};
            for (int i = 0; i < 4; ++i)
                palette_[i].target(targets[i]);
        }
        for (ui::SpringColor &colour : palette_)
            colour.update(dt, 3.0f);
    }

    void update_confirm(const InputFrame &input, app::Feedback &feedback)
    {
        if (input.is_pressed(Action::west))
        {
            for (store::Game &g : games_)
                if (g.id == confirm_.game_id)
                {
                    client_.remove(confirm_.id, g.title_id);
                    removal_titles_[confirm_.id] = g.title;
                    g.state = "removing"; // until the PC's list says otherwise
                    g.progress = 0.0f;
                }
            confirm_ = {};
            feedback.play(audio::Cue::select);
        }
        else if (input.is_pressed(Action::back))
        {
            confirm_ = {};
            feedback.play(audio::Cue::modal_close);
        }
    }

    void update_install(const InputFrame &input, app::Feedback &feedback)
    {
        store::Game *g = find(install_id_);
        const int rows = g != nullptr && g->kind == "game" ? drive_rows() : 0;
        drive_ = std::clamp(drive_, 0, std::max(0, rows - 1)); // the list can shrink while it's open
        if (g != nullptr && (input.nav == Direction::left || input.nav == Direction::right))
        {
            const std::vector<std::string> ids = copies(*g);
            if (ids.size() > 1) // another format of the same game
            {
                const auto at = std::find(ids.begin(), ids.end(), g->id) - ids.begin();
                const auto n = static_cast<std::ptrdiff_t>(ids.size());
                install_id_ = ids[static_cast<std::size_t>((at + (input.nav == Direction::right ? 1 : n - 1)) % n)];
                feedback.play(audio::Cue::focus);
                return;
            }
        }
        if (rows > 1 && (input.nav == Direction::up || input.nav == Direction::down))
        {
            const int next = std::clamp(drive_ + (input.nav == Direction::down ? 1 : -1), 0, rows - 1);
            if (next != drive_)
            {
                drive_ = next;
                feedback.play(audio::Cue::focus);
            }
            else if (!input.nav_repeat)
            {
                feedback.play(audio::Cue::error, 1.0f, 0.0f, 0.6f);
            }
        }
        if (g != nullptr && input.is_pressed(Action::confirm))
        {
            const store::Drive *drive = rows > 0 ? &drives_[static_cast<std::size_t>(drive_)] : nullptr;
            if (drive != nullptr && !fits(*drive, *g))
            {
                feedback.play(audio::Cue::error);
                return;
            }
            client_.install(g->id, drive != nullptr ? drive->id : std::string());
            g->state = "queued"; // until the PC's list says otherwise
            install_id_.clear();
            feedback.play(audio::Cue::select);
        }
        else if (g == nullptr || input.is_pressed(Action::back))
        {
            install_id_.clear();
            feedback.play(audio::Cue::modal_close);
        }
    }

    void update_stop(const InputFrame &input, app::Feedback &feedback)
    {
        if (input.is_pressed(Action::west))
        {
            client_.cancel(stop_id_);
            stop_id_.clear();
            feedback.play(audio::Cue::select);
        }
        else if (input.is_pressed(Action::back))
        {
            stop_id_.clear();
            feedback.play(audio::Cue::modal_close);
        }
    }

    void update_checkout(const InputFrame &input, app::Feedback &feedback)
    {
        const int rows = cart_games() > 0 ? drive_rows() : 0;
        drive_ = std::clamp(drive_, 0, std::max(0, rows - 1));
        if (rows > 1 && (input.nav == Direction::up || input.nav == Direction::down))
        {
            const int next = std::clamp(drive_ + (input.nav == Direction::down ? 1 : -1), 0, rows - 1);
            if (next != drive_)
            {
                drive_ = next;
                feedback.play(audio::Cue::focus);
            }
            else if (!input.nav_repeat)
            {
                feedback.play(audio::Cue::error, 1.0f, 0.0f, 0.6f);
            }
        }
        if (input.is_pressed(Action::confirm) && !cart_.empty())
        {
            const store::Drive *drive = rows > 0 ? &drives_[static_cast<std::size_t>(drive_)] : nullptr;
            if (drive != nullptr && !fits_bytes(*drive, cart_bytes(true)))
            {
                feedback.play(audio::Cue::error);
                return;
            }
            // The client sends each ahead of what's waiting, so the last goes first and they queue in cart order.
            for (auto it = cart_.rbegin(); it != cart_.rend(); ++it)
                if (store::Game *g = find(*it))
                {
                    client_.install(g->id, g->kind == "game" && drive != nullptr ? drive->id : std::string());
                    g->state = "queued"; // until the PC's list says otherwise
                }
            cart_.clear();
            checkout_ = false;
            tab_ = 1; // where they can be followed
            show_tab({});
            age_ = 0.0f;
            feedback.play(audio::Cue::select);
        }
        else if (cart_.empty() || input.is_pressed(Action::back))
        {
            checkout_ = false;
            feedback.play(audio::Cue::modal_close);
        }
    }

    void update_grid(const InputFrame &input, app::Feedback &feedback, int count)
    {
        const int tab_step = input.is_pressed(Action::page_next) ? 1 : input.is_pressed(Action::page_prev) ? kTabCount - 1 : 0;
        if (tab_step != 0)
        {
            tab_ = (tab_ + tab_step) % kTabCount;
            show_tab({});
            age_ = 0.0f; // the new tab's cards rise in (the shell plays the tab sound)
            if (tab_ == kLz4Tab)
                client_.refresh(); // what's on the console now
            return;
        }
        if (tab_ == kLz4Tab)
            return update_lz4(input, feedback);
        if (tab_ != kCartTab && input.is_pressed(Action::jump_prev))
        {
            sort_ = (sort_ + 1) % kSortCount;
            save::write_atomic(kSortFile, std::to_string(sort_));
            show_tab({});
            age_ = 0.0f; // the cards rise in again, in their new order
            feedback.play(audio::Cue::tab);
        }
        if (input.is_pressed(Action::north))
        {
            client_.refresh();
            feedback.play(audio::Cue::tick);
        }
        if (in_genres_)
            return update_genres(input, feedback);
        if (input.nav == Direction::up && focus_ < kColumns && !genres_.empty())
        {
            in_genres_ = true;
            feedback.play(audio::Cue::focus);
            return;
        }
        if (count > 0 && input.nav != Direction::none)
        {
            int next = focus_;
            if (input.nav == Direction::left && focus_ % kColumns > 0)
                --next;
            else if (input.nav == Direction::right && focus_ % kColumns < kColumns - 1)
                ++next;
            else if (input.nav == Direction::up)
                next -= kColumns;
            else if (input.nav == Direction::down)
                next += kColumns;
            next = std::clamp(next, 0, count - 1);
            if (next != focus_)
            {
                focus_ = next;
                feedback.play(audio::Cue::focus, 1.0f, ui::pan_for_x(card_rect(focus_).cx()));
            }
            else if (!input.nav_repeat)
            {
                feedback.play(audio::Cue::error, 1.0f, 0.0f, 0.6f);
                refusal_.trigger();
            }
        }
        if (tab_ == kCartTab && count > 0 && input.is_pressed(Action::confirm))
        {
            checkout_ = true;
            drive_ = most_free_drive();
            modal_.snap(0.0f);
            feedback.play(audio::Cue::modal_open);
        }
        else if (count > 0 && input.is_pressed(Action::confirm))
        {
            const store::Game &g = *focused();
            if (g.state == "installable")
            {
                install_id_ = g.id;
                drive_ = most_free_drive();
                modal_.snap(0.0f);
                feedback.play(audio::Cue::modal_open);
            }
            else
            {
                feedback.play(audio::Cue::error);
                refusal_.trigger();
            }
        }
        if (offline() && count > 0 && (input.is_pressed(Action::west) || input.is_pressed(Action::jump_next)))
        {
            message_ = input.is_pressed(Action::west) ? "Removing a game needs Spela running on your PC."
                                                      : "Favorites are kept on your PC: Spela needs to be running there.";
            message_ok_ = false;
            message_age_ = 0.0f;
            feedback.play(audio::Cue::error);
            refusal_.trigger();
            return;
        }
        if (count > 0 && input.is_pressed(Action::west))
        {
            const store::Game &g = *focused();
            if (removable(g) && awaiting_.empty())
            {
                awaiting_ = g.id;
                client_.preview_removal(g.id);
                feedback.play(audio::Cue::tick);
            }
            else if (installing(g))
            {
                stop_id_ = g.id;
                modal_.snap(0.0f);
                feedback.play(audio::Cue::modal_open);
            }
            else
            {
                feedback.play(audio::Cue::error);
                refusal_.trigger();
            }
        }
        if (count > 0 && input.is_pressed(Action::menu))
        {
            const store::Game &g = *focused();
            const auto at = std::find(cart_.begin(), cart_.end(), g.id);
            if (at != cart_.end())
            {
                cart_.erase(at);
                if (tab_ == kCartTab)
                    show_tab({}); // it leaves this tab
                feedback.play(audio::Cue::toggle);
            }
            else if (g.state == "installable")
            {
                cart_.push_back(g.id);
                feedback.play(audio::Cue::toggle);
            }
            else
            {
                feedback.play(audio::Cue::error);
                refusal_.trigger();
            }
        }
        if (count > 0 && input.is_pressed(Action::jump_next))
        {
            store::Game &g = games_[static_cast<std::size_t>(shown_[static_cast<std::size_t>(focus_)])];
            const bool on = !g.favorite;
            const store::Game was = g;
            for (store::Game &copy : games_)
                if (copy.id == was.id || same_game(copy, was))
                    copy.favorite = on; // until the PC's list says otherwise
            client_.favorite(was.id, on);
            show_tab(was.id); // it moves to the front or back, and the focus goes with it
            feedback.play(audio::Cue::toggle);
        }
    }

    // The PC hasn't answered since the app started, and the helper has: the Installed tab shows the helper's list.
    bool offline() const
    {
        return !loaded_ && !error_.empty() && console_loaded_;
    }

    // What's installed here as the Installed tab's cards, while the PC is away. No covers: they come from the PC.
    std::vector<store::Game> offline_games() const
    {
        std::vector<store::Game> games;
        for (const store::ConsoleGame &c : console_games_)
        {
            store::Game g;
            g.id = "console:" + c.path;
            g.title = c.title;
            g.title_id = c.title_id;
            g.version = c.version;
            g.state = "installed";
            g.format = c.kind == "package" ? "ps5_pkg" : c.kind == "image" ? "image" : "folder_dump";
            games.push_back(std::move(g));
        }
        return games;
    }

    const store::ConsoleGame *lz4_focused() const
    {
        return lz4_games_.empty() ? nullptr : &lz4_games_[static_cast<std::size_t>(lz4_focus_)];
    }

    // This game's job, while it runs or after it ended; null when the helper's job is another game's.
    const store::Pack *pack_of(const store::ConsoleGame &g) const
    {
        return !pack_.kind.empty() && pack_.game == g.path ? &pack_ : nullptr;
    }

    // Up and down pick a game; Cross packs it, Square deletes its set-aside original or stops the packing, Options
    // puts the original back. Each asks first (update_lz4_ask).
    void update_lz4(const InputFrame &input, app::Feedback &feedback)
    {
        if (input.is_pressed(Action::north))
        {
            client_.refresh();
            feedback.play(audio::Cue::tick);
        }
        const int count = static_cast<int>(lz4_games_.size());
        if (count > 0 && (input.nav == Direction::up || input.nav == Direction::down))
        {
            const int next = std::clamp(lz4_focus_ + (input.nav == Direction::down ? 1 : -1), 0, count - 1);
            if (next != lz4_focus_)
            {
                lz4_focus_ = next;
                feedback.play(audio::Cue::focus);
            }
            else if (!input.nav_repeat)
            {
                feedback.play(audio::Cue::error, 1.0f, 0.0f, 0.6f);
                refusal_.trigger();
            }
        }
        const store::ConsoleGame *g = lz4_focused();
        if (g == nullptr)
            return;
        const store::Pack *mine = pack_of(*g);
        const bool running_here = mine != nullptr && mine->running;
        const bool pressed = input.is_pressed(Action::confirm) || input.is_pressed(Action::west) || input.is_pressed(Action::menu);
        Lz4Ask ask = Lz4Ask::none;
        if (input.is_pressed(Action::confirm) && !g->packed && !g->original && !pack_.running)
            ask = Lz4Ask::pack;
        else if (input.is_pressed(Action::west) && running_here && mine->kind == "pack")
            ask = Lz4Ask::stop;
        else if (input.is_pressed(Action::west) && g->original && !pack_.running)
            ask = Lz4Ask::keep;
        else if (input.is_pressed(Action::menu) && g->original && !pack_.running)
            ask = Lz4Ask::restore;
        if (ask != Lz4Ask::none)
        {
            lz4_ask_ = ask;
            lz4_path_ = g->path;
            lz4_title_ = g->title;
            modal_.snap(0.0f);
            feedback.play(audio::Cue::modal_open);
        }
        else if (pressed)
        {
            feedback.play(audio::Cue::error);
            refusal_.trigger();
        }
    }

    // The same button again goes ahead; Circle doesn't.
    void update_lz4_ask(const InputFrame &input, app::Feedback &feedback)
    {
        const Action go = lz4_ask_ == Lz4Ask::pack ? Action::confirm : lz4_ask_ == Lz4Ask::restore ? Action::menu : Action::west;
        if (input.is_pressed(go))
        {
            if (lz4_ask_ == Lz4Ask::pack)
                client_.pack(lz4_path_);
            else if (lz4_ask_ == Lz4Ask::stop)
                client_.cancel_pack();
            else
                client_.finish_pack(lz4_path_, lz4_ask_ == Lz4Ask::keep);
            lz4_ask_ = Lz4Ask::none;
            feedback.play(audio::Cue::select);
        }
        else if (input.is_pressed(Action::back))
        {
            lz4_ask_ = Lz4Ask::none;
            feedback.play(audio::Cue::modal_close);
        }
    }

    // Left and right pick a genre and the grid follows; down, Cross or Circle go back to the games.
    void update_genres(const InputFrame &input, app::Feedback &feedback)
    {
        if (input.nav == Direction::left || input.nav == Direction::right)
        {
            const int at = genre_index();
            const int next = std::clamp(at + (input.nav == Direction::right ? 1 : -1), 0, static_cast<int>(genres_.size()));
            if (next != at)
            {
                genre_ = next == 0 ? std::string() : genres_[static_cast<std::size_t>(next - 1)];
                show_tab({});
                age_ = 0.0f;
                feedback.play(audio::Cue::focus, 1.0f, ui::pan_for_x(chip_rect(next).cx() - chips_scroll_.offset()));
            }
            else if (!input.nav_repeat)
            {
                feedback.play(audio::Cue::error, 1.0f, 0.0f, 0.6f);
                refusal_.trigger();
            }
        }
        else if (input.nav == Direction::down || input.is_pressed(Action::confirm) || input.is_pressed(Action::back))
        {
            in_genres_ = false;
            feedback.play(audio::Cue::focus);
        }
        else if (input.nav == Direction::up && !input.nav_repeat)
        {
            feedback.play(audio::Cue::error, 1.0f, 0.0f, 0.6f);
            refusal_.trigger();
        }
    }

    void draw(app::Frame &frame) const override
    {
        frame.backdrop.mode = gfx::BackdropMode::aurora;
        for (int i = 0; i < 4; ++i)
            frame.backdrop.colors[i] = palette_[i].value();
        frame.backdrop.time = context_.settings.reduced_motion ? 0.0f : clock_;
        frame.post.mode = gfx::BackdropMode::vignette;
        frame.post.colors[0] = kNight;
        frame.post.params[0] = 0.55f;

        gfx::DrawList &list = frame.scene;
        const ui::Fonts &fonts = context_.fonts;
        // A dialog pushes the screen back a little.
        list.push_transform(1.0f - 0.03f * modal_.value, 960, 540, 0, 0);
        draw_screen(list);
        list.pop_transform();
        if (dialog_open())
        {
            list.rounded_rect({0, 0, 1920, 1080}, 0, kNight.with_alpha(0.55f * modal_.value));
            frame.glass = true;
            if (lz4_ask_ != Lz4Ask::none)
                return draw_lz4_ask(frame.overlay, frame.glass_texture);
            if (!confirm_.id.empty())
                return draw_confirm(frame.overlay, frame.glass_texture);
            if (!done_.id.empty())
                return draw_done(frame.overlay, frame.glass_texture);
            if (!install_id_.empty())
                return draw_install(frame.overlay, frame.glass_texture);
            if (checkout_)
                return draw_checkout(frame.overlay, frame.glass_texture);
            return draw_stop(frame.overlay, frame.glass_texture);
        }
        if (client_.paired() && tab_ == kLz4Tab)
            return draw_lz4_hints(list);
        if (!client_.paired() || (!loaded_ && !offline()) || games_.empty() || (offline() && tab_ != kInstalledTab))
            return;
        ui::Hint hints[7];
        int count = 0;
        const store::Game *g = in_genres_ ? nullptr : focused(); // the genre row takes no game actions
        if (in_genres_)
            hints[count++] = {ui::Button::dpad, "Genre"};
        if (g != nullptr && tab_ == kCartTab)
            hints[count++] = {ui::Button::cross, "Install all"};
        else if (g != nullptr && g->state == "installable")
            hints[count++] = {ui::Button::cross, g->kind == "dlc" ? "Add" : "Install"};
        if (g != nullptr && in_cart(g->id))
            hints[count++] = {ui::Button::options, "Take out"};
        else if (g != nullptr && g->state == "installable")
            hints[count++] = {ui::Button::options, "Add to cart"};
        if (g != nullptr && removable(*g) && !offline())
            hints[count++] = {ui::Button::square, "Remove"};
        if (g != nullptr && installing(*g))
            hints[count++] = {ui::Button::square, "Stop"};
        if (g != nullptr && !offline())
            hints[count++] = {ui::Button::r2, g->favorite ? "Unfavorite" : "Favorite"};
        if (tab_ != kCartTab)
            hints[count++] = {ui::Button::l2, "Sort"};
        hints[count++] = {ui::Button::triangle, "Refresh"};
        hints[count++] = {ui::Button::l1, "Category", ui::Button::r1};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, count, 1824, true);
    }

    std::span<const app::TourStep> tour() const override
    {
        return kTour;
    }

  private:
    const store::Game *focused() const
    {
        return shown_.empty() ? nullptr : &games_[static_cast<std::size_t>(shown_[static_cast<std::size_t>(focus_)])];
    }

    store::Game *find(const std::string &id)
    {
        for (store::Game &g : games_)
            if (g.id == id)
                return &g;
        return nullptr;
    }

    const store::Game *find(const std::string &id) const
    {
        return const_cast<Storefront *>(this)->find(id);
    }

    bool dialog_open() const
    {
        return lz4_ask_ != Lz4Ask::none || !confirm_.id.empty() || !done_.id.empty() || !install_id_.empty() ||
               !stop_id_.empty() || checkout_;
    }

    bool in_cart(const std::string &id) const
    {
        return std::find(cart_.begin(), cart_.end(), id) != cart_.end();
    }

    // The cart's size; `games_only`: what lands on the picked drive (DLC goes onto its game's).
    std::uint64_t cart_bytes(bool games_only) const
    {
        std::uint64_t n = 0;
        for (const std::string &id : cart_)
            if (const store::Game *g = find(id); g != nullptr && (!games_only || g->kind == "game"))
                n += g->size_bytes;
        return n;
    }

    int cart_games() const
    {
        return static_cast<int>(std::count_if(cart_.begin(), cart_.end(), [this](const std::string &id) {
            const store::Game *g = find(id);
            return g != nullptr && g->kind == "game";
        }));
    }

    // The one with the most free space, like the PC's own pick.
    int most_free_drive() const
    {
        int best = 0;
        for (int i = 1; i < drive_rows(); ++i)
            if (drives_[static_cast<std::size_t>(i)].free_bytes > drives_[static_cast<std::size_t>(best)].free_bytes)
                best = i;
        return best;
    }

    int drive_rows() const
    {
        return std::min(kMaxDrives, static_cast<int>(drives_.size()));
    }

    // The same game in more than one format is several entries on the PC; the grid shows one card for them.
    bool same_game(const store::Game &a, const store::Game &b) const
    {
        return a.kind == "game" && b.kind == "game" && !a.title_id.empty() && a.title_id == b.title_id;
    }

    // Which copy a title's card stands for: one in the cart, then one doing something (installed, installing...), then
    // a real install over a backup archive, then the PC's order.
    bool is_card(std::size_t i) const
    {
        const auto rank = [this](const store::Game &g) {
            return std::make_tuple(!in_cart(g.id), g.state == "installable" || g.state == "blocked", g.format == "archive");
        };
        for (std::size_t j = 0; j < games_.size(); ++j)
            if (j != i && same_game(games_[i], games_[j]) &&
                (rank(games_[j]) < rank(games_[i]) || (rank(games_[j]) == rank(games_[i]) && j < i)))
                return false;
        return true;
    }

    // The ids of the formats that can be installed for this game, the game itself included.
    std::vector<std::string> copies(const store::Game &g) const
    {
        std::vector<std::string> ids;
        for (const store::Game &c : games_)
            if (c.id == g.id || (same_game(g, c) && c.state == "installable"))
                ids.push_back(c.id);
        return ids;
    }

    // Favorites first, then by sort_. The PC sends the list by name.
    bool before(const store::Game &a, const store::Game &b) const
    {
        if (a.favorite != b.favorite)
            return a.favorite;
        if (sort_ == 1)
            return a.added > b.added;
        if (sort_ == 2)
            return a.size_bytes > b.size_bytes;
        return false;
    }

    // Lists the open tab's games in genre_, keeping the focus on `keep` when it's still there; and the tab's genres,
    // most common first. A genre that's no longer in the tab goes back to all of them.
    void show_tab(const std::string &keep)
    {
        shown_.clear();
        if (tab_ == kLz4Tab || (offline() && tab_ != kInstalledTab)) // its own list, or the PC's library while it's away
        {
            genres_.clear();
            in_genres_ = false;
            return;
        }
        std::unordered_map<std::string, int> counts;
        for (std::size_t i = 0; i < games_.size(); ++i)
        {
            const store::Game &g = games_[i];
            if (tab_ == kCartTab ? !in_cart(g.id) : !in_tab(g, tab_) || !is_card(i))
                continue;
            if (tab_ != kCartTab)
                for (const std::string &genre : g.genres)
                    ++counts[genre];
            if (tab_ == kCartTab || genre_.empty() || std::find(g.genres.begin(), g.genres.end(), genre_) != g.genres.end())
                shown_.push_back(static_cast<int>(i));
        }
        if (!genre_.empty() && !counts.contains(genre_))
        {
            genre_.clear();
            return show_tab(keep);
        }
        genres_.clear();
        for (const auto &[genre, n] : counts)
            genres_.push_back(genre);
        std::sort(genres_.begin(), genres_.end(), [&counts](const std::string &a, const std::string &b) {
            return counts[a] != counts[b] ? counts[a] > counts[b] : a < b;
        });
        in_genres_ = in_genres_ && !genres_.empty();
        if (tab_ != kCartTab) // the cart keeps the order things went in
            std::stable_sort(shown_.begin(), shown_.end(), [this](int a, int b) {
                return before(games_[static_cast<std::size_t>(a)], games_[static_cast<std::size_t>(b)]);
            });
        const auto at = std::find_if(shown_.begin(), shown_.end(),
                                     [&](int i) { return games_[static_cast<std::size_t>(i)].id == keep; });
        focus_ = at != shown_.end() ? static_cast<int>(at - shown_.begin()) : 0;
    }

    // 0 is All, then genres_.
    int genre_index() const
    {
        const auto at = std::find(genres_.begin(), genres_.end(), genre_);
        return genre_.empty() || at == genres_.end() ? 0 : static_cast<int>(at - genres_.begin()) + 1;
    }

    const std::string &genre_name(int i) const
    {
        static const std::string kAll = "All";
        return i == 0 ? kAll : genres_[static_cast<std::size_t>(i - 1)];
    }

    // Where chip i sits before the row scrolls.
    Rect chip_rect(int i) const
    {
        const ui::FontRef &font = context_.fonts.semibold;
        float x = kMargin;
        for (int j = 0; j < i; ++j)
            x += font.measure(genre_name(j), 22) + 40 + 12;
        return {x, kGenresY, font.measure(genre_name(i), 22) + 40, kChipH};
    }

    void draw_genres(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        list.push_clip({kMargin - 12, kGenresY - 12, kPanelX - kGap - kMargin + 12, kChipH + 24});
        const int picked = genre_index();
        for (int i = 0; i <= static_cast<int>(genres_.size()); ++i)
        {
            Rect chip = chip_rect(i);
            chip.x -= chips_scroll_.offset();
            if (chip.x > kPanelX)
                break;
            const bool on = i == picked;
            if (on && in_genres_)
                chip.x += ui::shake(refusal_.value, clock_);
            list.rounded_rect(chip, kChipH * 0.5f, on ? kAccent.with_alpha(in_genres_ ? 0.95f : 0.3f) : kWhite.with_alpha(0.07f));
            list.bordered_rect(chip, kChipH * 0.5f, kTransparent, on && in_genres_ ? 2.0f : 1.5f,
                               kWhite.with_alpha(on && in_genres_ ? 0.9f : 0.12f));
            ui::text(list, fonts.semibold, genre_name(i), chip.cx(), chip.y + 30, 22,
                     on ? kInk : kMuted.with_alpha(in_genres_ ? 1.0f : 0.8f), gfx::Align::center);
        }
        list.pop_clip();
    }

    void draw_tabs(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const ui::GlyphStyle style = ui::GlyphStyle::dark();
        float x = kMargin;
        ui::draw_button(list, fonts, style, ui::Button::l1, x, 134, 34);
        x += ui::button_width(ui::Button::l1, 34) + 28;
        for (int i = 0; i < kTabCount; ++i)
        {
            const float w = fonts.semibold.measure(kTabs[i], 44);
            ui::text(list, fonts.semibold, kTabs[i], x, 150, 44, i == tab_ ? kInk : kMuted.with_alpha(0.7f));
            if (i == tab_)
                list.rounded_rect({x, 166, w, 5}, 2.5f, kAccent);
            x += w + 44;
        }
        ui::draw_button(list, fonts, style, ui::Button::r1, x - 16, 134, 34);
    }

    void draw_screen(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        if (logo_ != 0)
            list.image(logo_, {kMargin, 40, kLogoWidth, kLogoHeight}, gfx::kFullUv, kWhite);
        else // no GPU batch yet
            ui::text(list, fonts.semibold, "SPELA", kMargin + 2, 84, 18, kInk.with_alpha(0.55f),
                     gfx::Align::left, 4.0f);
        if (client_.paired())
            draw_tabs(list);
        else
            ui::text(list, fonts.display, "Your games", kMargin, 150, 56, kInk);
        if (client_.paired() && tab_ == kLz4Tab)
            return draw_lz4(list);

        if (!client_.paired())
            return draw_notice(list, "Not paired with a PC",
                               "Install it again from Spela on your PC: open Settings, find this console under Consoles, and click Install PS5 app.");
        if (!loaded_ && !offline())
            return error_.empty()
                       ? draw_notice(list, "Connecting to your PC...", "Through the Spela helper on this PS5.")
                       : draw_notice(list, "Can't reach Spela", error_);
        if (offline() && tab_ != kInstalledTab)
        {
            draw_notice(list, "Library offline",
                        "Your PC's library shows here while Spela runs on your PC. Installed and LZ4 show what's on this "
                        "PS5 without it.");
            return draw_message(list);
        }
        if (offline() && games_.empty())
            return draw_notice(list, "Nothing installed", "No games are installed on this PS5.");
        if (games_.empty())
            return draw_notice(list, "No games on your PC yet",
                               "On the PC, follow the setup guide on Spela's Dashboard, or add your games folder in Settings and scan it.");

        char count[80];
        if (tab_ == kCartTab)
            std::snprintf(count, sizeof(count), "%zu %s", shown_.size(), kTabCounts[tab_]);
        else
            std::snprintf(count, sizeof(count), "%zu %s  ·  %s", shown_.size(), kTabCounts[tab_], kSorts[sort_]);
        const float count_w = fonts.semibold.measure(count, 22) + 40;
        const Rect pill{kPanelX - kGap - count_w, 112, count_w, 44};
        list.rounded_rect(pill, 22, kWhite.with_alpha(0.08f));
        list.bordered_rect(pill, 22, kTransparent, 1.5f, kWhite.with_alpha(0.14f));
        ui::text(list, fonts.semibold, count, pill.cx(), pill.y + 30, 22, kInk, gfx::Align::center);
        if (offline())
            ui::text(list, fonts.regular, "Offline: what's installed on this PS5. Your library comes back once Spela runs on your PC.",
                     kMargin, kGenresY + 30, 24, kMuted);
        else if (!error_.empty()) // a later poll failed; the last good list stays up
            ui::text(list, fonts.regular, list_fit(fonts, error_, 24, 1200), kMargin, kGenresY + 30, 24, kBad); // in the genres' place

        if (shown_.empty())
        {
            const char *empty = tab_ == 0   ? "No games on your PC yet, only DLC. On the PC, add your games folder in Settings and scan it."
                                : tab_ == 1 ? "Nothing from your PC is on this PS5 yet. Install a game from Games."
                                : tab_ == 2 ? "No DLC on your PC. On the PC, add a DLC folder under Settings and scan it."
                                            : "Your cart is empty. In Games or DLC, press Options on the ones you want, "
                                              "then come back here and install them all at once.";
            ui::paragraph(list, fonts.regular, empty, kMargin, kGridTop + 60, 28, 1200, 40, kMuted, 3);
        }
        else
        {
            draw_grid(list);
            draw_panel(list);
        }
        if (tab_ != kCartTab && !genres_.empty() && error_.empty())
            draw_genres(list);
        draw_message(list);
    }

    void draw_message(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        if (!message_.empty() && message_age_ < kMessageSeconds)
        {
            const float fade = std::min(1.0f, (kMessageSeconds - message_age_) * 2.0f);
            list.push_opacity(fade);
            ui::text(list, fonts.semibold, list_fit(fonts, message_, 26, 1100), kMargin, 950, 26, // above the hints
                     message_ok_ ? kGood : kBad);
            list.pop_opacity();
        }
    }

    Tones tones(const store::Game &g) const
    {
        const auto found = tones_.find(g.id);
        return found != tones_.end() ? found->second : placeholder_tones(g.title);
    }

    static Rect card_rect(int index)
    {
        const int row = index / kColumns;
        const int column = index % kColumns;
        return {kMargin + static_cast<float>(column) * (kCardW + kGap),
                kGridTop + static_cast<float>(row) * kPitch, kCardW, kCardH};
    }

    std::string list_fit(const ui::Fonts &fonts, const std::string &text, float size, float width) const
    {
        return fonts.regular.font->fit(text, size, width);
    }

    void take_covers()
    {
        std::string id;
        std::vector<std::uint8_t> rgba;
        while (client_.take_cover(&id, &rgba))
        {
            gfx::GlBatch *batch = store::texture_batch();
            // ponytail: textures are never freed; a few hundred covers is ~100 MB of GPU memory.
            if (batch == nullptr)
                continue;
            covers_[id] = batch->create_texture(store::kCoverWidth, store::kCoverHeight, rgba.data());
            constexpr int kHalf = store::kCoverHeight / 2;
            tones_[id] = {average(rgba, 0, kHalf), average(rgba, kHalf, store::kCoverHeight)};
        }
        while (client_.take_logo(&id, &rgba))
            if (gfx::GlBatch *batch = store::texture_batch())
                logos_[id] = batch->create_texture(store::kTitleLogoWidth, store::kTitleLogoHeight, rgba.data());
    }

    // Covers for the rows on screen and the next two, each asked for once.
    void request_covers()
    {
        if (!client_.paired() || store::texture_batch() == nullptr)
            return;
        const int first_row = static_cast<int>(scroll_.offset() / kPitch);
        const int end = std::min(static_cast<int>(shown_.size()), (first_row + 5) * kColumns);
        for (int i = first_row * kColumns; i < end; ++i)
        {
            const store::Game &g = games_[static_cast<std::size_t>(shown_[static_cast<std::size_t>(i)])];
            if (g.has_cover && requested_.insert(g.id).second)
                client_.request_cover(g.id);
        }
        // the logo only shows in the focused game's panel, so only that one's is asked for
        if (const store::Game *g = focused(); g != nullptr && g->has_logo && logos_requested_.insert(g->id).second)
            client_.request_logo(g->id);
    }

    void draw_notice(gfx::DrawList &list, const char *title, const std::string &detail) const
    {
        const ui::Fonts &fonts = context_.fonts;
        list.glow({960 - 40, 360, 80, 80}, 40, 60, kAccent.with_alpha(0.25f + 0.2f * ui::breathe(clock_)));
        list.circle(960, 400, 40, kWhite.with_alpha(0.06f));
        list.ring(960, 400, 40, 3, kAccent.with_alpha(0.8f));
        ui::text(list, fonts.semibold, title, 960, 500, 44, kInk, gfx::Align::center);
        ui::paragraph(list, fonts.regular, detail, 960, 560, 28, 1200, 40, kMuted, 6, // centred: x is each line's middle
                      gfx::Align::center);
        const ui::Hint hints[] = {{ui::Button::triangle, "Try again"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 1, 1824, true);
    }

    // A frosted dialog box that grows in; end_dialog() closes what it opened.
    Rect draw_dialog(gfx::DrawList &list, std::uint32_t glass, float top, float height, Color edge) const
    {
        const float t = modal_.value;
        const Rect box{960 - 520, top, 1040, height};
        list.push_opacity(std::min(1.0f, t * 1.5f));
        list.push_transform(0.94f + 0.06f * t, box.cx(), box.cy(), 0, 24.0f * (1.0f - t));
        list.shadow({box.x, box.y + 24, box.w, box.h}, 28, 70, Color::rgb(0x000000, 0.6f));
        list.glass(glass, box, 28, kWhite);
        list.rounded_rect(box, 28, Color::rgb(0x12152a, 0.72f));
        list.gradient_rect(box, 28, edge.with_alpha(0.16f), kTransparent);
        list.bordered_rect(box, 28, kTransparent, 1.5f, edge.with_alpha(0.4f));
        return box;
    }

    static void end_dialog(gfx::DrawList &list)
    {
        list.pop_transform();
        list.pop_opacity();
    }

    void draw_confirm(gfx::DrawList &list, std::uint32_t glass) const
    {
        const ui::Fonts &fonts = context_.fonts;
        std::string title = "this game";
        for (const store::Game &g : games_)
            if (g.id == confirm_.game_id)
                title = g.title_id.empty() ? g.title : g.title + " (" + g.title_id + ")";
        const Rect box = draw_dialog(list, glass, 300, 480, kBad);
        const float x = box.x + 48;
        const float width = box.w - 96;
        float y = ui::paragraph(list, fonts.semibold, "Remove " + title + "?", x, box.y + 76, 36, width, 46, kInk, 2);
        y = ui::paragraph(list, fonts.regular, "This deletes its files from this PS5:", x, y + 12, 24, width, 34,
                          kMuted);
        y = ui::paragraph(list, fonts.mono, confirm_.path, x, y + 4, 22, width, 32, kInk, 2);
        const std::string space = confirm_.bytes ? "Frees about " + store::format_size(confirm_.bytes) + ". " : "";
        y = ui::paragraph(list, fonts.regular,
                          space + "Save data and the copy on your PC are kept. Its tile stays on the home screen "
                                  "until you delete it there, and won't come back.",
                          x, y + 20, 24, width, 34, kMuted, 4);
        if (!confirm_.warnings.empty())
            ui::paragraph(list, fonts.regular, confirm_.warnings, x, y + 12, 24, width, 34, kWarn, 4);
        end_dialog(list);
        const ui::Hint hints[] = {{ui::Button::square, "Remove"}, {ui::Button::circle, "Keep"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 2, 1824, true);
    }

    void draw_done(gfx::DrawList &list, std::uint32_t glass) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const std::string title = done_title_.empty() ? std::string("the game") : done_title_;
        const Color edge = !done_.ok ? kBad : done_.tile_left ? kWarn : kGood;
        const Rect box = draw_dialog(list, glass, 270, 540, edge); // room for 8 lines: the tile's reason comes last
        const float x = box.x + 48;
        const float width = box.w - 96;
        const float y = ui::paragraph(list, fonts.semibold, (done_.ok ? "Removed " : "Couldn't remove ") + title,
                                      x, box.y + 76, 36, width, 46, done_.ok ? kInk : kBad, 2);
        ui::paragraph(list, fonts.regular, done_.message, x, y + 12, 26, width, 38,
                      done_.ok && !done_.tile_left ? kMuted : done_.ok ? kWarn : kBad, 8);
        end_dialog(list);
        const ui::Hint hints[] = {{ui::Button::cross, "OK"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 1, 1824, true);
    }

    // The game, the drives to pick from, and what to know before it starts.
    void draw_install(gfx::DrawList &list, std::uint32_t glass) const
    {
        const store::Game *g = find(install_id_);
        if (g == nullptr)
            return;
        const ui::Fonts &fonts = context_.fonts;
        const bool dlc = g->kind == "dlc";
        const bool package = g->format == "ps5_pkg" || g->format == "ps4_pkg";
        const char *note = package ? "This sends a package file to the PS5 package installer. Check the title ID and "
                                     "version above before you continue."
                           : dlc   ? "Its files are copied into the installed game's folder and replace files there "
                                     "with the same names. Close the game first. To undo it, remove the game and "
                                     "install it again."
                           : g->format == "archive" ? "It's unpacked on your PC first, so the PC needs room for the "
                                                      "unpacked game."
                                                    : "";
        const int rows = dlc ? 0 : drive_rows();
        const bool no_drive = !dlc && rows == 0;
        const std::vector<std::string> versions = copies(*g);
        const bool pick_version = versions.size() > 1;
        const float height = 250.0f + 68.0f * static_cast<float>(rows) + (no_drive ? 80.0f : 0.0f) +
                             (*note != '\0' ? 150.0f : 0.0f) + (pick_version ? 44.0f : 0.0f);
        const Rect box = draw_dialog(list, glass, 540 - height * 0.5f, height, kAccent);
        const float x = box.x + 48;
        const float width = box.w - 96;
        float y = ui::paragraph(list, fonts.semibold, (dlc ? "Add " : "Install ") + g->title + (dlc ? " to its game?" : "?"),
                                x, box.y + 76, 36, width, 46, kInk, 2);
        std::string meta = g->title_id.empty() ? std::string("Title ID unknown") : g->title_id;
        if (!g->version.empty())
            meta += "  ·  v" + g->version;
        ui::text(list, fonts.regular, meta + "  ·  " + store::format_size(g->size_bytes), x, y + 30, 24, kMuted);
        y += 50;
        if (pick_version)
        {
            const auto at = std::find(versions.begin(), versions.end(), g->id) - versions.begin();
            const std::string what = g->format == "folder_dump" ? "Folder" : g->format == "ps5_pkg" || g->format == "ps4_pkg" ? "Package"
                                     : g->format == "image"     ? "Disk image"
                                     : g->format == "archive"   ? "Archive"
                                                                : g->format;
            ui::text(list, fonts.semibold, "<  " + what + "  >  " + std::to_string(at + 1) + " of " + std::to_string(versions.size()),
                     x, y + 30, 24, kAccent);
            y += 44;
        }
        if (no_drive)
            y = ui::paragraph(list, fonts.regular,
                              "No drive on this PS5 is set for installs. On the PC, open Installed, click a drive and tick Allow installs here.",
                              x, y + 30, 24, width, 34, kWarn, 2);
        for (int i = 0; i < rows; ++i)
        {
            const store::Drive &d = drives_[static_cast<std::size_t>(i)];
            const Rect row{x, y + 16 + 68.0f * static_cast<float>(i), width, 56};
            const bool on = i == drive_;
            list.rounded_rect(row, 14, kWhite.with_alpha(on ? 0.12f : 0.05f));
            if (on)
                list.bordered_rect(row, 14, kTransparent, 2, kAccent);
            ui::text(list, fonts.semibold, d.label, row.x + 20, row.y + 37, 24, on ? kInk : kMuted);
            const bool ok = fits(d, *g);
            const std::string space = d.total_bytes == 0 ? std::string("Free space unknown")
                                      : ok               ? store::format_size(d.free_bytes) + " free"
                                                         : "Not enough space";
            ui::text(list, fonts.regular, space, row.x + row.w - 20, row.y + 37, 24, ok ? kMuted : kBad, gfx::Align::right);
        }
        y += 68.0f * static_cast<float>(rows);
        if (*note != '\0')
            ui::paragraph(list, fonts.regular, note, x, y + 40, 24, width, 34, kWarn, 4);
        end_dialog(list);
        ui::Hint hints[4];
        int count = 0;
        hints[count++] = {ui::Button::cross, dlc ? "Add" : "Install"};
        if (pick_version)
            hints[count++] = {ui::Button::dpad, "Format"};
        if (rows > 1)
            hints[count++] = {ui::Button::dpad, "Drive"};
        hints[count++] = {ui::Button::circle, "Cancel"};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, count, 1824, true);
    }

    // What's in the cart, its size, and the drive the games go to.
    void draw_checkout(gfx::DrawList &list, std::uint32_t glass) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const int names = std::min(kCheckoutNames, static_cast<int>(cart_.size()));
        const bool more = static_cast<int>(cart_.size()) > names;
        const int rows = cart_games() > 0 ? drive_rows() : 0;
        const bool no_drive = cart_games() > 0 && rows == 0;
        const float height = 250.0f + 38.0f * static_cast<float>(names + (more ? 1 : 0)) + 68.0f * static_cast<float>(rows) +
                             (no_drive ? 80.0f : 0.0f);
        const Rect box = draw_dialog(list, glass, 540 - height * 0.5f, height, kAccent);
        const float x = box.x + 48;
        const float width = box.w - 96;
        char title[64];
        std::snprintf(title, sizeof(title), cart_.size() == 1 ? "Install 1 item?" : "Install all %zu items?", cart_.size());
        float y = ui::paragraph(list, fonts.semibold, title, x, box.y + 76, 36, width, 46, kInk, 1);
        ui::text(list, fonts.regular,
                 store::format_size(cart_bytes(false)) + " in all, copied one after another in this order.",
                 x, y + 30, 24, kMuted);
        y += 54;
        for (int i = 0; i < names; ++i)
            if (const store::Game *g = find(cart_[static_cast<std::size_t>(i)]))
            {
                ui::text(list, fonts.regular, fonts.regular.font->fit(g->title, 24, width - 220), x + 20, y + 30, 24, kInk);
                ui::text(list, fonts.regular,
                         g->kind == "dlc" ? "DLC  ·  " + store::format_size(g->size_bytes) : store::format_size(g->size_bytes),
                         x + width, y + 30, 24, kMuted, gfx::Align::right);
                y += 38;
            }
        if (more)
        {
            char rest[48];
            std::snprintf(rest, sizeof(rest), "and %zu more", cart_.size() - static_cast<std::size_t>(names));
            ui::text(list, fonts.regular, rest, x + 20, y + 30, 24, kMuted);
            y += 38;
        }
        y += 16;
        if (no_drive)
            ui::paragraph(list, fonts.regular,
                          "No drive on this PS5 is set for installs. On the PC, open Installed, click a drive and tick Allow installs here.",
                          x, y + 30, 24, width, 34, kWarn, 2);
        const std::uint64_t need = cart_bytes(true);
        for (int i = 0; i < rows; ++i)
        {
            const store::Drive &d = drives_[static_cast<std::size_t>(i)];
            const Rect row{x, y + 16 + 68.0f * static_cast<float>(i), width, 56};
            const bool on = i == drive_;
            list.rounded_rect(row, 14, kWhite.with_alpha(on ? 0.12f : 0.05f));
            if (on)
                list.bordered_rect(row, 14, kTransparent, 2, kAccent);
            ui::text(list, fonts.semibold, d.label, row.x + 20, row.y + 37, 24, on ? kInk : kMuted);
            const bool ok = fits_bytes(d, need);
            const std::string space = d.total_bytes == 0 ? std::string("Free space unknown")
                                      : ok               ? store::format_size(d.free_bytes) + " free"
                                                         : "Not enough space for all of them";
            ui::text(list, fonts.regular, space, row.x + row.w - 20, row.y + 37, 24, ok ? kMuted : kBad, gfx::Align::right);
        }
        end_dialog(list);
        ui::Hint hints[3];
        int count = 0;
        hints[count++] = {ui::Button::cross, "Install all"};
        if (rows > 1)
            hints[count++] = {ui::Button::dpad, "Drive"};
        hints[count++] = {ui::Button::circle, "Not yet"};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, count, 1824, true);
    }

    void draw_stop(gfx::DrawList &list, std::uint32_t glass) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const store::Game *g = find(stop_id_);
        const Rect box = draw_dialog(list, glass, 400, 240, kWarn);
        const float x = box.x + 48;
        const float width = box.w - 96;
        const float y = ui::paragraph(list, fonts.semibold,
                                      "Stop installing " + (g != nullptr ? g->title : std::string("this game")) + "?",
                                      x, box.y + 76, 36, width, 46, kInk, 2);
        ui::paragraph(list, fonts.regular,
                      g != nullptr && g->state == "queued" ? "It hasn't started yet, so nothing has been copied."
                                                           : "The copy stops at its next step. To finish it later, "
                                                             "install it again.",
                      x, y + 12, 24, width, 34, kMuted, 3);
        end_dialog(list);
        const ui::Hint hints[] = {{ui::Button::square, "Stop"}, {ui::Button::circle, "Keep installing"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 2, 1824, true);
    }

    // The LZ4 tab: the console's game folders on the left, the focused one on the right.
    void draw_lz4(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        if (!console_loaded_)
        {
            draw_notice(list, "Asking the helper...", "The LZ4 tab needs the Spela helper running on this PS5. If this stays, "
                                                      "run your jailbreak, or update Spela on your PC.");
            return draw_message(list);
        }
        if (lz4_games_.empty())
        {
            ui::paragraph(list, fonts.regular, "No game folders on this PS5's drives. Only games installed as folders, in "
                                               "a homebrew folder, can be packed here.",
                          kMargin, kGridTop + 60, 28, 1200, 40, kMuted, 3);
            return draw_message(list);
        }
        if (pack_.running) // whichever game it is
        {
            const int percent = pack_.total ? static_cast<int>(pack_.done * 100 / pack_.total) : 0;
            std::string busy = std::string(pack_phase(pack_.phase)) + ": " + pack_.game.substr(pack_.game.rfind('/') + 1);
            if (pack_.total)
                busy += "  ·  " + std::to_string(percent) + "%";
            ui::text(list, fonts.semibold, list_fit(fonts, busy, 24, 1150), kMargin, kGenresY + 30, 24, kWarn);
        }
        const int visible = static_cast<int>((kGridBottom - kGridTop) / kRowH);
        const int first = std::max(0, lz4_focus_ - visible + 1);
        for (int i = first; i < std::min(static_cast<int>(lz4_games_.size()), first + visible); ++i)
        {
            const store::ConsoleGame &g = lz4_games_[static_cast<std::size_t>(i)];
            Rect row{kMargin, kGridTop + static_cast<float>(i - first) * kRowH, kPanelX - kGap - kMargin, kRowH - 12};
            const bool on = i == lz4_focus_;
            if (on)
                row.x += ui::shake(refusal_.value, clock_);
            list.rounded_rect(row, 16, on ? kWhite.with_alpha(0.12f) : kWhite.with_alpha(0.04f));
            if (on)
                list.bordered_rect(row.inset(-4), 20, kTransparent, 3, kWhite.with_alpha(0.75f + 0.25f * ui::breathe(clock_)));
            ui::text(list, fonts.semibold, list_fit(fonts, g.title, 28, row.w - 330), row.x + 24, row.y + 46, 28, kInk);
            const store::Pack *mine = pack_of(g);
            const StateLook state = mine != nullptr && mine->running ? StateLook{"Working", kWarn}
                                    : g.original                     ? StateLook{"Original kept", kWarn}
                                    : g.packed                       ? StateLook{"Packed", kGood}
                                                                     : StateLook{g.title_id.c_str(), kMuted};
            ui::text(list, fonts.semibold, state.label, row.x + row.w - 24, row.y + 46, 22, state.color, gfx::Align::right);
        }
        draw_lz4_panel(list);
        draw_message(list);
    }

    void draw_lz4_panel(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const store::ConsoleGame &g = *lz4_focused();
        const store::Pack *mine = pack_of(g);
        const Rect panel{kPanelX, kPanelTop, kPanelW, kGridBottom - kPanelTop};
        list.shadow({panel.x, panel.y + 20, panel.w, panel.h}, 24, 50, Color::rgb(0x000000, 0.45f));
        list.rounded_rect(panel, 24, kPanel.with_alpha(0.7f));
        list.bordered_rect(panel, 24, kTransparent, 1.5f, kWhite.with_alpha(0.12f));
        const float x = panel.x + 36;
        const float width = panel.w - 72;
        float y = panel.y + 70;
        const auto lines = ui::paragraph_lines(fonts.semibold, g.title, 36, width, 3);
        ui::paragraph(list, fonts.semibold, lines, x, y, 36, 46, kInk);
        y += 46.0f * static_cast<float>(lines.lines.size()) + 8;
        ui::text(list, fonts.regular, g.title_id, x, y, 24, kMuted);
        y += 34;
        ui::paragraph(list, fonts.mono, g.path, x, y, 20, width, 28, kMuted, 2);
        y += 80;
        std::string text;
        Color colour = kMuted;
        if (mine != nullptr && mine->running)
        {
            const float progress = mine->total ? static_cast<float>(mine->done) / static_cast<float>(mine->total) : 0.0f;
            draw_progress(list, {x, y - 6, width, 12}, progress);
            char percent[16];
            std::snprintf(percent, sizeof(percent), "%d%%", static_cast<int>(progress * 100.0f + 0.5f));
            ui::text(list, fonts.mono, percent, x + width, y - 44, 22, kInk, gfx::Align::right);
            text = std::string(pack_phase(mine->phase)) + ". " +
                   (mine->kind == "pack" ? "It carries on with your PC off. Press Square to stop." : "");
        }
        else if (mine != nullptr && mine->phase == "failed")
        {
            text = mine->error;
            colour = kBad;
        }
        else if (mine != nullptr && mine->phase == "canceled")
        {
            text = "Packing was stopped. The game is as it was.";
        }
        else if (pack_.running && (g.original || !g.packed))
        {
            text = "This PS5 is busy with " + pack_.game.substr(pack_.game.rfind('/') + 1) +
                   ". Wait for it to finish, then come back to this one.";
        }
        else if (g.original)
        {
            text = (mine != nullptr && mine->phase == "done"
                        ? "Packed: " + store::format_size(mine->after) + ", was " + store::format_size(mine->before) + ". "
                        : std::string("Packed with LZ4. ")) +
                   "The original is kept, hidden, so it still takes its space. Start the game: if it runs, press Square to "
                   "delete the original. If it doesn't, press Options to put the original back.";
        }
        else if (g.packed)
        {
            text = mine != nullptr && mine->phase == "deleted" ? "The original is deleted. The packed game stays."
                                                               : "Packed with LZ4.";
        }
        else
        {
            text = (mine != nullptr && mine->phase == "restored" ? std::string("The original is back. ") : std::string()) +
                   "Press Cross to pack it with LZ4 on this PS5, often to about half its size. Only for games that use "
                   "AMPR. Your PC can be off while it runs.";
        }
        ui::paragraph(list, fonts.regular, text, x, y + 40, 24, width, 34, colour, 8);
    }

    void draw_lz4_hints(gfx::DrawList &list) const
    {
        ui::Hint hints[6];
        int count = 0;
        if (const store::ConsoleGame *g = console_loaded_ ? lz4_focused() : nullptr)
        {
            const store::Pack *mine = pack_of(*g);
            if (!g->packed && !g->original && !pack_.running)
                hints[count++] = {ui::Button::cross, "Pack with LZ4"};
            if (mine != nullptr && mine->running && mine->kind == "pack")
                hints[count++] = {ui::Button::square, "Stop"};
            else if (g->original && !pack_.running)
            {
                hints[count++] = {ui::Button::square, "Delete original"};
                hints[count++] = {ui::Button::options, "Put original back"};
            }
        }
        hints[count++] = {ui::Button::triangle, "Refresh"};
        hints[count++] = {ui::Button::l1, "Category", ui::Button::r1};
        ui::draw_hints(list, context_.fonts, ui::GlyphStyle::dark(), hints, count, 1824, true);
    }

    void draw_lz4_ask(gfx::DrawList &list, std::uint32_t glass) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const bool risky = lz4_ask_ == Lz4Ask::keep || lz4_ask_ == Lz4Ask::restore;
        const float height = lz4_ask_ == Lz4Ask::pack ? 330.0f : 250.0f; // the pack question says the most
        const Rect box = draw_dialog(list, glass, 540 - height * 0.5f, height, risky ? kBad : kAccent);
        const float x = box.x + 48;
        const float width = box.w - 96;
        const std::string title = lz4_ask_ == Lz4Ask::pack    ? "Pack " + lz4_title_ + " with LZ4?"
                                  : lz4_ask_ == Lz4Ask::stop  ? "Stop packing " + lz4_title_ + "?"
                                  : lz4_ask_ == Lz4Ask::keep  ? "Delete the original of " + lz4_title_ + "?"
                                                              : "Put the original of " + lz4_title_ + " back?";
        const char *text =
            lz4_ask_ == Lz4Ask::pack ? "This PS5 packs it by itself, which takes a while for a big game. Your PC can be off. The "
                                       "drive needs free space for about half the game. The original is kept, hidden, until you "
                                       "delete it or put it back. Some games that use AMPR still won't start packed."
            : lz4_ask_ == Lz4Ask::stop ? "What it packed so far is deleted. The game stays as it was."
            : lz4_ask_ == Lz4Ask::keep ? "Only do this once the packed game has started. It frees the original's space, and "
                                         "can't be undone."
                                       : "The packed copy is deleted and the original takes its place again. Close the game first.";
        const float y = ui::paragraph(list, fonts.semibold, title, x, box.y + 76, 36, width, 46, kInk, 2);
        ui::paragraph(list, fonts.regular, text, x, y + 12, 24, width, 34, kMuted, 6);
        end_dialog(list);
        const ui::Hint hints[] = {
            lz4_ask_ == Lz4Ask::pack      ? ui::Hint{ui::Button::cross, "Pack"}
            : lz4_ask_ == Lz4Ask::restore ? ui::Hint{ui::Button::options, "Put back"}
            : lz4_ask_ == Lz4Ask::keep    ? ui::Hint{ui::Button::square, "Delete"}
                                          : ui::Hint{ui::Button::square, "Stop"},
            {ui::Button::circle, lz4_ask_ == Lz4Ask::stop ? "Keep packing" : "Cancel"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 2, 1824, true);
    }

    void draw_grid(gfx::DrawList &list) const
    {
        list.push_clip({0, kGenresY + kChipH + 4, kPanelX - 8, kGridBottom + 20 - (kGenresY + kChipH + 4)});
        list.push_transform(1.0f, 0.0f, 0.0f, 0.0f, -scroll_.offset());
        const int count = static_cast<int>(shown_.size());
        for (int i = 0; i < count; ++i)
            if (i != focus_)
                draw_card(list, i);
        draw_card(list, focus_); // last, so it rises over its neighbours
        Rect ring = focus_ring_.value().inset(-6);
        ring.x += ui::shake(refusal_.value, clock_);
        if (!in_genres_) // the genre row has the focus
            list.bordered_rect(ring, 20, kTransparent, 3, kWhite.with_alpha(0.75f + 0.25f * ui::breathe(clock_)));
        list.pop_transform();
        list.pop_clip();
    }

    void draw_card(gfx::DrawList &list, int i) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const Rect r = card_rect(i);
        if (r.y - scroll_.offset() > kGridBottom + 40 || r.y + kPitch - scroll_.offset() < kGridTop - 40)
            return;
        const store::Game &g = games_[static_cast<std::size_t>(shown_[static_cast<std::size_t>(i)])];
        const Tones colours = tones(g);
        const bool focused = i == focus_;
        const float lift = focused ? lift_.value : 0.0f;
        // Cards rise into place one after another when the library arrives.
        const float appear = context_.settings.reduced_motion ? 1.0f : tween::stagger(age_, i, 0.035f, 0.45f);
        list.push_opacity(appear);
        list.push_transform(1.0f + kLift * lift, r.cx(), r.cy(), 0, 40.0f * (1.0f - appear));

        list.shadow({r.x, r.y + 12 + 14 * lift, r.w, r.h}, 14, 26 + 20 * lift, Color::rgb(0x000000, 0.5f));
        if (focused)
            list.glow(r, 14, 46 * lift, gfx::mix(colours.top, kWhite, 0.35f).with_alpha(0.55f * lift));
        const Color tint = focused ? kWhite : Color::rgb(0xbfc3d4);
        const auto cover = covers_.find(g.id);
        if (cover != covers_.end())
        {
            list.image(cover->second, r, gfx::kFullUv, tint, 14);
        }
        else
        {
            list.gradient_rect(r, 14, colours.top, colours.bottom);
            const std::string initial = g.title.substr(0, 1);
            ui::text(list, fonts.display, initial, r.cx(), r.cy() + 34, 110, kWhite.with_alpha(0.75f),
                     gfx::Align::center);
        }
        // A sheen across the top edge makes the card read as a glossy box.
        list.gradient_rect({r.x, r.y, r.w, r.h * 0.4f}, 14, kWhite.with_alpha(0.12f), kTransparent);
        list.bordered_rect(r, 14, kTransparent, 1.0f, kWhite.with_alpha(focused ? 0.3f : 0.1f));

        const StateLook state = in_cart(g.id) ? StateLook{"In cart", kAccent} : look(g);
        if (g.state != "installable" || in_cart(g.id))
        {
            const Rect badge{r.x + 10, r.y + 10, fonts.semibold.measure(state.label, 17) + 40, 32};
            list.rounded_rect(badge, 16, kNight.with_alpha(0.82f));
            list.glow({badge.x + 13, badge.y + 11, 10, 10}, 5, 6, state.color.with_alpha(0.6f));
            list.circle(badge.x + 18, badge.cy(), 5, state.color);
            ui::text(list, fonts.semibold, state.label, badge.x + 30, badge.y + 22, 17, kInk);
        }
        if (g.state == "installing" || g.state == "removing")
        {
            list.gradient_rect({r.x, r.y + r.h - 60, r.w, 60}, 14, kTransparent, kNight.with_alpha(0.8f));
            draw_progress(list, {r.x + 14, r.y + r.h - 22, r.w - 28, 8}, g.progress);
        }
        list.pop_transform();
        const float title_y = r.y + r.h + 36 + r.h * kLift * 0.5f * lift;
        const float heart = g.favorite ? 28.0f : 0.0f; // a favorite's title starts with a heart
        if (g.favorite)
            draw_heart(list, r.x + 10, title_y - 8, 22, kFavorite);
        ui::text(list, fonts.semibold, fonts.semibold.font->fit(g.title, 22, kCardW - heart), r.x + heart, title_y, 22,
                 focused ? kInk : kMuted);
        list.pop_opacity();
    }

    // A bar that fills with a warm gradient and glows at its leading edge.
    void draw_progress(gfx::DrawList &list, const Rect &bar, float progress) const
    {
        const float radius = bar.h * 0.5f;
        list.rounded_rect(bar, radius, kWhite.with_alpha(0.14f));
        const Rect fill{bar.x, bar.y, std::max(bar.h, bar.w * progress), bar.h};
        list.glow(fill, radius, 10 + 4 * ui::breathe(clock_, 1.4f), kWarn.with_alpha(0.45f));
        list.gradient_rect_h(fill, radius, Color::rgb(0xff8a3d), Color::rgb(0xffd166));
    }

    void draw_panel(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const store::Game &g = *focused();
        const Rect panel{kPanelX, kPanelTop, kPanelW, kGridBottom - kPanelTop};
        const Tones colours = tones(g);
        list.shadow({panel.x, panel.y + 20, panel.w, panel.h}, 24, 50, Color::rgb(0x000000, 0.45f));
        list.rounded_rect(panel, 24, kPanel.with_alpha(0.7f));
        // The focused game's art, fading into the panel. Its top part: key art keeps faces and titles near the top.
        const Rect art{panel.x, panel.y, panel.w, kArtH};
        const auto cover = covers_.find(g.id);
        if (cover != covers_.end())
        {
            const float shown = kArtH / (panel.w * store::kCoverHeight / store::kCoverWidth);
            list.image_gradient(cover->second, art, {0, 0, 1, shown}, kWhite, kWhite.with_alpha(0.0f), 24);
        }
        else
        {
            list.gradient_rect(art, 24, colours.top, colours.bottom.with_alpha(0.0f));
            ui::text(list, fonts.display, g.title.substr(0, 1), art.cx(), art.cy() + 50, 150, kWhite.with_alpha(0.35f),
                     gfx::Align::center);
        }
        list.gradient_rect({panel.x, panel.y, panel.w, kArtH + 40}, 24, kTransparent, kPanel.with_alpha(0.6f));
        list.bordered_rect(panel, 24, kTransparent, 1.5f, kWhite.with_alpha(0.12f));
        const float x = panel.x + 36;
        const float width = panel.w - 72;
        float y = panel.y + kArtH + 30;
        const auto logo = logos_.find(g.id);
        if (logo != logos_.end())
        {
            // The store's logo instead of the title, its bottom where a one-line title's would be: the PC sets it in
            // the bottom-left corner of its box, so a tall one reaches up into the art's faded lower edge.
            list.image(logo->second, {x, y + 10 - store::kTitleLogoHeight, store::kTitleLogoWidth, store::kTitleLogoHeight},
                       gfx::kFullUv, kWhite);
            y += 46.0f + 8;
        }
        else
        {
            const auto lines = ui::paragraph_lines(fonts.semibold, g.title, 36, width, 3);
            ui::paragraph(list, fonts.semibold, lines, x, y, 36, 46, kInk);
            y += 46.0f * static_cast<float>(lines.lines.size()) + 8;
        }

        std::string meta = g.title_id.empty() ? std::string("Title ID unknown") : g.title_id;
        if (!g.version.empty())
            meta += "  ·  v" + g.version;
        ui::text(list, fonts.regular, meta, x, y, 24, kMuted);
        y += 40;
        if (g.size_bytes > 0) // unknown offline
            ui::text(list, fonts.regular, store::format_size(g.size_bytes), x, y, 24, kMuted);
        y += 56;

        const StateLook state = in_cart(g.id) ? StateLook{"In cart", kAccent} : look(g);
        const Rect pill{x, y - 32, fonts.semibold.measure(state.label, 24) + 52, 46};
        list.rounded_rect(pill, 23, state.color.with_alpha(0.16f));
        list.bordered_rect(pill, 23, kTransparent, 1.5f, state.color.with_alpha(0.45f));
        list.glow({pill.x + 16, pill.cy() - 6, 12, 12}, 6, 8, state.color.with_alpha(0.7f));
        list.circle(pill.x + 22, pill.cy(), 6, state.color);
        ui::text(list, fonts.semibold, state.label, pill.x + 38, pill.y + 32, 24, kInk);
        y += 44;
        if (g.state == "installing" || g.state == "removing")
        {
            draw_progress(list, {x, y - 6, width, 12}, g.progress);
            char percent[16];
            std::snprintf(percent, sizeof(percent), "%d%%", static_cast<int>(g.progress * 100.0f + 0.5f));
            ui::text(list, fonts.mono, percent, x + width, y - 44, 22, kInk, gfx::Align::right);
            // How far, how fast, how long: from the PC's job, about once a second.
            if (g.bytes_total > 0)
            {
                y += 40;
                ui::text(list, fonts.regular, store::format_size(g.bytes_done) + " of " + store::format_size(g.bytes_total),
                         x, y, 22, kInk);
                if (g.rate_bps > 0)
                    ui::text(list, fonts.mono, store::format_size(g.rate_bps) + "/s", x + width, y, 22, kInk,
                             gfx::Align::right);
            }
            if (g.state == "installing" && g.seconds_left >= 0 && g.rate_bps > 0)
            {
                y += 32;
                ui::text(list, fonts.regular, store::format_duration(g.seconds_left), x, y, 22, kMuted);
            }
            if (g.bytes_total > 0)
                y += 10;
        }
        const char *detail = tab_ == kCartTab ? "Press Cross to install everything in the cart. Options takes this one out."
                             : in_cart(g.id)  ? "It's in your cart. Install everything in it from the Cart tab."
                             : g.state == "installable"
                                 ? g.kind == "dlc" ? "Press Cross to add it to its game on this console, or Options to put it in the cart."
                                                   : "Press Cross to copy it from your PC to this console, or Options to put it in the cart."
                             : g.state == "installed" && offline() ? "It's on this PS5. Removing it needs Spela running on your PC."
                             : g.state == "installed"  ? "It's on the home screen. Press Square to remove it."
                             : g.state == "update"     ? "Your PC has a newer version than the one installed."
                             : g.state == "queued"     ? "Your PC will start copying it soon. Press Square to cancel."
                             : g.state == "installing" ? "Press Square to stop."
                             : g.state == "removing"   ? "Your PC is deleting it from this console, then its tile."
                                                       : nullptr;
        std::string text = detail != nullptr ? std::string(detail) : g.reason;
        if (!g.step.empty() && g.state == "installing")
            text = g.step + " " + text; // "Copying to USB. Press Square to stop."
        else if (!g.step.empty() && g.state == "removing")
            text = g.step;
        ui::paragraph(list, fonts.regular, text, x, y + 40, 24, width, 34, kMuted, 4);
    }

    app::Context &context_;
    store::Client client_;
    std::vector<store::ConsoleGame> console_games_; // what's installed here, from the helper
    std::vector<store::ConsoleGame> lz4_games_;     // its game folders: the LZ4 tab's
    store::Pack pack_;
    bool console_loaded_ = false; // the helper has answered
    int lz4_focus_ = 0;
    Lz4Ask lz4_ask_ = Lz4Ask::none; // the LZ4 tab's question, shown while set
    std::string lz4_path_;          // the game it's about
    std::string lz4_title_;
    std::vector<store::Game> games_;
    std::vector<store::Drive> drives_;
    std::vector<int> shown_; // indexes into games_ of the open tab; focus_ indexes this
    int tab_ = 0;            // kTabs
    int sort_ = 0;           // kSorts
    std::vector<std::string> genres_; // the open tab's, most common first
    std::string genre_;               // the one shown; empty: all of them
    bool in_genres_ = false;          // the focus is on the genre row, not the grid
    ui::Scroller chips_scroll_;
    std::string install_id_; // the game the install dialog is for; shown while set
    int drive_ = 0;          // the drive picked in it
    std::string stop_id_;    // the install the stop dialog is for; shown while set
    std::vector<std::string> cart_; // game ids in the order they were added; installable ones only
    bool checkout_ = false;         // the install-all dialog is open
    std::string error_;
    bool loaded_ = false;
    std::unordered_map<std::string, std::uint32_t> covers_;
    std::uint32_t logo_ = 0; // the wordmark's texture, made once the GPU batch exists
    std::unordered_map<std::string, Tones> tones_;
    std::unordered_set<std::string> requested_;
    std::unordered_map<std::string, std::uint32_t> logos_; // game id -> its store logo's texture
    std::unordered_set<std::string> logos_requested_;
    std::string message_;
    bool message_ok_ = false;
    float message_age_ = kMessageSeconds;
    std::string awaiting_;   // the game whose removal preview was asked for
    store::Removal confirm_; // shown while its id is set
    store::RemovalResult done_; // how a removal ended; shown while its id is set
    std::string done_title_;
    std::unordered_map<std::string, std::string> removal_titles_; // removal id -> game title
    int focus_ = 0;
    float age_ = 0.0f;
    float clock_ = 0.0f;
    ui::SpringRect focus_ring_;
    ui::Scroller scroll_;
    ui::Pulse refusal_;
    tween::Spring lift_;  // 0..1: the focused card growing
    tween::Spring modal_; // 0..1: a dialog coming in
    ui::SpringColor palette_[4];
};

} // namespace

std::unique_ptr<app::Concept> make_storefront(app::Context &context)
{
    return std::make_unique<Storefront>(context);
}

} // namespace hui::concepts
