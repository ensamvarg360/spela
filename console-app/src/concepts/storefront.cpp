// PS5 Local Storefront - The games on your PC: browse them, install and remove them.
// SPDX-License-Identifier: GPL-3.0-or-later
//
// A grid of covers on the left and the focused game on the right. Cross asks
// the PC to install it; the PC does the copying and this screen follows its
// progress. Square asks the PC what removing an installed game would delete,
// shows it, and Square again removes it; a dialog says how it ended, and
// whether its home-screen tile still has to be deleted by hand. All network
// traffic goes through store::Client on its own thread.

#include "concepts/concepts.hpp"

#include "gfx/gl_batch.hpp"
#include "store/client.hpp"
#include "ui/glyphs.hpp"
#include "ui/motion.hpp"

#include <algorithm>
#include <cstdio>
#include <string>
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

constexpr float kMargin = 96.0f;
constexpr int kColumns = 5;
constexpr float kCardW = 216.0f;
constexpr float kCardH = kCardW * 320.0f / 256.0f; // the PC's cover shape
constexpr float kGap = 32.0f;
constexpr float kPitch = kCardH + 84.0f;
constexpr float kGridTop = 210.0f;
constexpr float kGridBottom = 950.0f;
constexpr float kPanelX = 1368.0f;
constexpr float kPanelW = 1920.0f - kMargin - kPanelX;
constexpr float kMessageSeconds = 5.0f;

constexpr const char *kTechniques[] = {
    "Library from the PC app over HTTP, on a background thread",
    "Covers arrive as raw RGBA and become textures as they load",
    "Installs run on the PC; this screen polls their progress",
    "Removals show the PC's preview and wait for a second press",
};

// Shown in the PC preview: the tour waits, moves, and installs one game.
constexpr app::TourStep kTour[] = {
    {1.0f, 0, Direction::none, "grid"},
    {0.3f, 0, Direction::down},
    {0.8f, action_bit(Action::confirm), Direction::none, "focus"},
    {1.2f, 0, Direction::none, "queued"},
    {0.3f, 0, Direction::up},
    {0.3f, 0, Direction::right},
    {0.8f, action_bit(Action::west), Direction::none},
    {0.8f, 0, Direction::none, "remove"},
    {0.2f, action_bit(Action::west), Direction::none},
    {0.6f, 0, Direction::none, "removing"},
    {0.2f, action_bit(Action::north), Direction::none}, // the preview outruns the poll timer
    {0.8f, 0, Direction::none, "removed"},
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

bool removable(const store::Game &g)
{
    return g.state == "installed" || g.state == "update";
}

class Storefront final : public app::Concept
{
  public:
    explicit Storefront(app::Context &context) : context_(context)
    {
        focus_ring_.snap(card_rect(0));
    }

    const app::ConceptInfo &info() const override
    {
        static const app::ConceptInfo kInfo{
            "storefront",
            "PS5 Local Storefront",
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

        std::string error;
        if (client_.take_library(&games_, &error))
        {
            loaded_ = loaded_ || error.empty();
            error_ = error;
            focus_ = std::min(focus_, std::max(0, static_cast<int>(games_.size()) - 1));
        }
        std::string text;
        bool ok = false;
        while (client_.take_message(&text, &ok))
        {
            message_ = text;
            message_ok_ = ok;
            message_age_ = 0.0f;
            feedback.play(ok ? audio::Cue::complete : audio::Cue::error);
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
                feedback.play(audio::Cue::modal_open);
            }
        }

        store::RemovalResult result;
        while (done_.id.empty() && client_.take_removal_result(&result))
        {
            done_ = result;
            done_title_ = removal_titles_[result.id];
            removal_titles_.erase(result.id);
            client_.refresh(); // the game's new state
            feedback.play(!result.ok ? audio::Cue::error : result.tile_left ? audio::Cue::notify : audio::Cue::complete);
        }

        if (!confirm_.id.empty())
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
        else
        {
            update_grid(input, feedback, static_cast<int>(games_.size()));
        }

        request_covers();
        const Rect card = card_rect(focus_);
        scroll_.reveal(card.y - kGridTop, card.y - kGridTop + kPitch, kGridBottom - kGridTop, 24.0f);
        scroll_.update(dt);
        focus_ring_.target(card);
        focus_ring_.update(dt, 20.0f);
        refusal_.update(dt, 9.0f);
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

    void update_grid(const InputFrame &input, app::Feedback &feedback, int count)
    {
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
        if (count > 0 && input.is_pressed(Action::confirm))
        {
            store::Game &g = games_[static_cast<std::size_t>(focus_)];
            if (g.state == "installable")
            {
                client_.install(g.id);
                g.state = "queued"; // until the PC's list says otherwise
                feedback.play(audio::Cue::select);
            }
            else
            {
                feedback.play(audio::Cue::error);
                refusal_.trigger();
            }
        }
        if (count > 0 && input.is_pressed(Action::west))
        {
            const store::Game &g = games_[static_cast<std::size_t>(focus_)];
            if (removable(g) && awaiting_.empty())
            {
                awaiting_ = g.id;
                client_.preview_removal(g.id);
                feedback.play(audio::Cue::tick);
            }
            else
            {
                feedback.play(audio::Cue::error);
                refusal_.trigger();
            }
        }
        if (input.is_pressed(Action::north))
        {
            client_.refresh();
            feedback.play(audio::Cue::tick);
        }
    }

    void draw(app::Frame &frame) const override
    {
        frame.backdrop.mode = gfx::BackdropMode::gradient;
        frame.backdrop.colors[0] = Color::rgb(0x0b0e1a);
        frame.backdrop.colors[1] = Color::rgb(0x161034);
        frame.backdrop.time = clock_;

        gfx::DrawList &list = frame.scene;
        const ui::Fonts &fonts = context_.fonts;
        ui::text(list, fonts.display, "Your games", kMargin, 150, 56, kInk);

        if (!client_.paired())
            return draw_notice(list, "Not paired with a PC",
                               "Install this app from the PC app: open Installed and click Install PS5 app.");
        if (!loaded_)
            return error_.empty()
                       ? draw_notice(list, "Connecting to your PC...", client_.server().url)
                       : draw_notice(list, "Can't reach your PC", error_);
        if (games_.empty())
            return draw_notice(list, "No games on your PC yet",
                               "On the PC, add a folder under Settings and scan it.");

        char count[64];
        std::snprintf(count, sizeof(count), "%zu games", games_.size());
        ui::text(list, fonts.regular, count, kPanelX - kGap, 150, 26, kMuted, gfx::Align::right);
        if (!error_.empty()) // a later poll failed; the last good list stays up
            ui::text(list, fonts.regular, list_fit(fonts, error_, 24, 1200), kMargin, 190, 24, kBad);

        draw_grid(list);
        draw_panel(list);

        if (!message_.empty() && message_age_ < kMessageSeconds)
        {
            const float fade = std::min(1.0f, (kMessageSeconds - message_age_) * 2.0f);
            list.push_opacity(fade);
            ui::text(list, fonts.semibold, list_fit(fonts, message_, 26, 1100), kMargin, 1020, 26,
                     message_ok_ ? kGood : kBad);
            list.pop_opacity();
        }
        if (!confirm_.id.empty())
            return draw_confirm(list);
        if (!done_.id.empty())
            return draw_done(list);
        const bool can_remove = removable(games_[static_cast<std::size_t>(focus_)]);
        const ui::Hint hints[] = {{ui::Button::cross, "Install"},
                                  {ui::Button::triangle, "Refresh"},
                                  {ui::Button::square, "Remove"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, can_remove ? 3 : 2, 1824, true);
    }

    std::span<const app::TourStep> tour() const override
    {
        return kTour;
    }

  private:
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
            if (batch != nullptr)
                covers_[id] = batch->create_texture(store::kCoverWidth, store::kCoverHeight, rgba.data());
        }
    }

    // Covers for the rows on screen and the next two, each asked for once.
    void request_covers()
    {
        if (!client_.paired() || store::texture_batch() == nullptr)
            return;
        const int first_row = static_cast<int>(scroll_.offset() / kPitch);
        const int end = std::min(static_cast<int>(games_.size()), (first_row + 5) * kColumns);
        for (int i = first_row * kColumns; i < end; ++i)
        {
            const store::Game &g = games_[static_cast<std::size_t>(i)];
            if (g.has_cover && requested_.insert(g.id).second)
                client_.request_cover(g.id);
        }
    }

    void draw_notice(gfx::DrawList &list, const char *title, const std::string &detail) const
    {
        const ui::Fonts &fonts = context_.fonts;
        ui::text(list, fonts.semibold, title, 960, 500, 44, kInk, gfx::Align::center);
        ui::paragraph(list, fonts.regular, detail, 960 - 600, 560, 28, 1200, 40, kMuted, 4,
                      gfx::Align::center);
        const ui::Hint hints[] = {{ui::Button::triangle, "Try again"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 1, 1824, true);
    }

    void draw_confirm(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        std::string title = "this game";
        for (const store::Game &g : games_)
            if (g.id == confirm_.game_id)
                title = g.title_id.empty() ? g.title : g.title + " (" + g.title_id + ")";
        list.rounded_rect({0, 0, 1920, 1080}, 0, Color::rgb(0x05070d, 0.75f));
        const Rect box{960 - 520, 300, 1040, 480};
        list.rounded_rect(box, 24, Color::rgb(0x161a2b));
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
        const ui::Hint hints[] = {{ui::Button::square, "Remove"}, {ui::Button::circle, "Keep"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 2, 1824, true);
    }

    void draw_done(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const std::string title = done_title_.empty() ? std::string("the game") : done_title_;
        list.rounded_rect({0, 0, 1920, 1080}, 0, Color::rgb(0x05070d, 0.75f));
        const Rect box{960 - 520, 340, 1040, 400};
        list.rounded_rect(box, 24, Color::rgb(0x161a2b));
        const float x = box.x + 48;
        const float width = box.w - 96;
        const float y = ui::paragraph(list, fonts.semibold, (done_.ok ? "Removed " : "Couldn't remove ") + title,
                                      x, box.y + 76, 36, width, 46, done_.ok ? kInk : kBad, 2);
        ui::paragraph(list, fonts.regular, done_.message, x, y + 12, 26, width, 38,
                      done_.ok && !done_.tile_left ? kMuted : done_.ok ? kWarn : kBad, 6);
        const ui::Hint hints[] = {{ui::Button::cross, "OK"}};
        ui::draw_hints(list, fonts, ui::GlyphStyle::dark(), hints, 1, 1824, true);
    }

    void draw_grid(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        list.push_clip({0, kGridTop - 40, kPanelX - 8, kGridBottom - kGridTop + 80});
        list.push_transform(1.0f, 0.0f, 0.0f, 0.0f, -scroll_.offset());
        const int count = static_cast<int>(games_.size());
        for (int i = 0; i < count; ++i)
        {
            const Rect r = card_rect(i);
            if (r.y - scroll_.offset() > kGridBottom + 40 || r.y + kPitch - scroll_.offset() < kGridTop - 40)
                continue;
            const store::Game &g = games_[static_cast<std::size_t>(i)];
            const auto cover = covers_.find(g.id);
            if (cover != covers_.end())
            {
                list.image(cover->second, r, gfx::kFullUv, Color::rgb(0xffffff), 14);
            }
            else
            {
                list.rounded_rect(r, 14, Color::rgb(0x232a45));
                const std::string initial = g.title.substr(0, 1);
                ui::text(list, fonts.display, initial, r.cx(), r.cy() + 30, 96, kMuted, gfx::Align::center);
            }
            const StateLook state = look(g);
            if (g.state != "installable")
            {
                const Rect badge{r.x + 10, r.y + 10, fonts.semibold.measure(state.label, 18) + 24, 32};
                list.rounded_rect(badge, 16, Color::rgb(0x0b0e1a, 0.85f));
                ui::text(list, fonts.semibold, state.label, badge.x + 12, badge.y + 22, 18, state.color);
            }
            if (g.state == "installing" || g.state == "removing")
            {
                const Rect bar{r.x + 12, r.y + r.h - 22, r.w - 24, 10};
                list.rounded_rect(bar, 5, Color::rgb(0x0b0e1a, 0.85f));
                list.rounded_rect({bar.x, bar.y, std::max(10.0f, bar.w * g.progress), bar.h}, 5, kWarn);
            }
            const Color title = i == focus_ ? kInk : kMuted;
            ui::text(list, fonts.semibold, fonts.semibold.font->fit(g.title, 22, kCardW), r.x, r.y + r.h + 36,
                     22, title);
        }
        Rect ring = focus_ring_.value().inset(-7);
        ring.x += ui::shake(refusal_.value, clock_);
        list.bordered_rect(ring, 20, kTransparent, 4, kInk);
        list.pop_transform();
        list.pop_clip();
    }

    void draw_panel(gfx::DrawList &list) const
    {
        const ui::Fonts &fonts = context_.fonts;
        const store::Game &g = games_[static_cast<std::size_t>(focus_)];
        const Rect panel{kPanelX, kGridTop - 40, kPanelW, kGridBottom - kGridTop + 40};
        list.rounded_rect(panel, 24, kPanel);
        const float x = panel.x + 36;
        const float width = panel.w - 72;
        float y = panel.y + 70;
        const auto lines = ui::paragraph_lines(fonts.semibold, g.title, 36, width, 3);
        ui::paragraph(list, fonts.semibold, lines, x, y, 36, 46, kInk);
        y += 46.0f * static_cast<float>(lines.lines.size()) + 16;

        std::string meta = g.title_id.empty() ? std::string("Title ID unknown") : g.title_id;
        if (!g.version.empty())
            meta += "  ·  v" + g.version;
        ui::text(list, fonts.regular, meta, x, y, 24, kMuted);
        y += 40;
        ui::text(list, fonts.regular, store::format_size(g.size_bytes), x, y, 24, kMuted);
        y += 56;

        const StateLook state = look(g);
        ui::text(list, fonts.semibold, state.label, x, y, 28, state.color);
        y += 44;
        if (g.state == "installing" || g.state == "removing")
        {
            const Rect bar{x, y - 16, width, 14};
            list.rounded_rect(bar, 7, Color::rgb(0x0b0e1a));
            list.rounded_rect({bar.x, bar.y, std::max(14.0f, bar.w * g.progress), bar.h}, 7, kWarn);
            char percent[16];
            std::snprintf(percent, sizeof(percent), "%d%%", static_cast<int>(g.progress * 100.0f + 0.5f));
            ui::text(list, fonts.mono, percent, x + width, y - 44, 22, kMuted, gfx::Align::right);
        }
        const char *detail = g.state == "installable"  ? "Press Cross to copy it from your PC to this console."
                             : g.state == "installed"  ? "It's on the home screen. Press Square to remove it."
                             : g.state == "update"     ? "Your PC has a newer version than the one installed."
                             : g.state == "queued"     ? "Your PC will start copying it soon."
                             : g.state == "installing" ? ""
                             : g.state == "removing"   ? "Your PC is deleting it from this console, then its tile."
                                                       : nullptr;
        ui::paragraph(list, fonts.regular, detail != nullptr ? std::string(detail) : g.reason, x, y + 40,
                      24, width, 34, kMuted, 6);
    }

    app::Context &context_;
    store::Client client_;
    std::vector<store::Game> games_;
    std::string error_;
    bool loaded_ = false;
    std::unordered_map<std::string, std::uint32_t> covers_;
    std::unordered_set<std::string> requested_;
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
};

} // namespace

std::unique_ptr<app::Concept> make_storefront(app::Context &context)
{
    return std::make_unique<Storefront>(context);
}

} // namespace hui::concepts
