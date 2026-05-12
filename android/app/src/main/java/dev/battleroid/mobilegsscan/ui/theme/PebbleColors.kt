package dev.battleroid.mobilegsscan.ui.theme

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/**
 * Pebble palette — kept in lockstep with `web/src/app/globals.css`
 * and the design source at `tokens.jsx:9` (`PALETTES.studio`).
 * Every name on the web side maps to a `Pebble*` color here, so a
 * cross-platform tweak only needs the two files in sync.
 *
 * Material3's [androidx.compose.material3.ColorScheme] is too lossy
 * for our token surface — it offers ~20 semantic slots, and a few
 * of our distinctions (chipN, accent2/3, inkSoft vs inkMuted) don't
 * have first-class equivalents. We map onto its slots where the
 * shoe fits (so default-themed M3 widgets at least look on-brand)
 * and expose the full Pebble surface via [LocalPebbleColors] for
 * the bespoke composables that need it.
 */
object PebbleColors {
    val Bg = Color(0xFFFBF6EC)         // warm cream paper
    val BgAlt = Color(0xFFF2EAD8)      // toasted cream
    val Surface = Color(0xFFFFFFFF)
    val Ink = Color(0xFF1A1612)        // espresso ink
    val InkSoft = Color(0xFF5C5347)    // muted clay
    val InkMuted = Color(0xFF8B8273)   // dust
    val Rule = Color(0xFFE5DCC8)       // soft beige rule
    val RuleStrong = Color(0xFFC9BCA1)
    val Accent = Color(0xFFFF5A36)     // tomato (PRIMARY)
    val Accent2 = Color(0xFF7B5BFF)    // electric plum
    val Accent3 = Color(0xFF22C58A)    // sage green
    val Warn = Color(0xFFF0A500)
    val Danger = Color(0xFFE03A2F)
    val Chip1 = Color(0xFFFFD9C9)      // peach
    val Chip2 = Color(0xFFE0D6FF)      // lavender
    val Chip3 = Color(0xFFC7EBD9)      // mint
    val Chip4 = Color(0xFFFFE9A8)      // butter
}

/**
 * Bundle the full Pebble surface so composables don't have to
 * import the singleton — they pull from `MaterialTheme.pebble`
 * (an extension on [androidx.compose.material3.MaterialTheme]).
 * Held as a stable data class so swapping themes (a future
 * Arcade direction, dark mode, etc.) updates downstream readers.
 */
data class PebblePalette(
    val bg: Color,
    val bgAlt: Color,
    val surface: Color,
    val ink: Color,
    val inkSoft: Color,
    val inkMuted: Color,
    val rule: Color,
    val ruleStrong: Color,
    val accent: Color,
    val accent2: Color,
    val accent3: Color,
    val warn: Color,
    val danger: Color,
    val chip1: Color,
    val chip2: Color,
    val chip3: Color,
    val chip4: Color,
)

val PebbleStudioPalette = PebblePalette(
    bg = PebbleColors.Bg,
    bgAlt = PebbleColors.BgAlt,
    surface = PebbleColors.Surface,
    ink = PebbleColors.Ink,
    inkSoft = PebbleColors.InkSoft,
    inkMuted = PebbleColors.InkMuted,
    rule = PebbleColors.Rule,
    ruleStrong = PebbleColors.RuleStrong,
    accent = PebbleColors.Accent,
    accent2 = PebbleColors.Accent2,
    accent3 = PebbleColors.Accent3,
    warn = PebbleColors.Warn,
    danger = PebbleColors.Danger,
    chip1 = PebbleColors.Chip1,
    chip2 = PebbleColors.Chip2,
    chip3 = PebbleColors.Chip3,
    chip4 = PebbleColors.Chip4,
)

/**
 * Static because the palette is fixed for a given PebbleTheme tree
 * — there's no Pebble-internal state that should invalidate readers
 * on change. If we ever add a runtime theme switcher (Arcade vs
 * Studio, or DayNight), this flips to [compositionLocalOf].
 */
val LocalPebbleColors = staticCompositionLocalOf<PebblePalette> {
    error(
        "PebblePalette accessed outside PebbleTheme — wrap your " +
            "composable in PebbleTheme { ... } so LocalPebbleColors " +
            "has a value."
    )
}
