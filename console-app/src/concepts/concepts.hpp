// PS5 Local Storefront - Factories of the app's screens.
// SPDX-License-Identifier: GPL-3.0-or-later

#pragma once

#include "app/concept.hpp"

#include <memory>

namespace hui::concepts
{

std::unique_ptr<app::Concept> make_storefront(app::Context &context);

} // namespace hui::concepts
