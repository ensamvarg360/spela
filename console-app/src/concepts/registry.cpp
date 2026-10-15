// PS5 Local Storefront - The app has one screen.
// SPDX-License-Identifier: GPL-3.0-or-later

#include "concepts/concepts.hpp"

namespace hui::app
{

std::span<const ConceptFactory> concept_registry()
{
    static constexpr ConceptFactory kFactories[] = {concepts::make_storefront};
    return kFactories;
}

} // namespace hui::app
