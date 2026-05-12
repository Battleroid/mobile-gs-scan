package dev.battleroid.mobilegsscan.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.ReadOnlyComposable
import androidx.compose.ui.unit.dp

/**
 * Pebble corner-radius scale — mirrors `tailwind.config.ts`
 * (`RADIUS_SCALES[1]` = "Soft"). Pinned here so PR-A's design
 * choice doesn't drift across PRs; later screens read shapes from
 * `MaterialTheme.shapes` rather than literal `RoundedCornerShape`
 * calls.
 */
private val PebbleShapes = Shapes(
    extraSmall = RoundedCornerShape(4.dp),
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(12.dp),
    large = RoundedCornerShape(18.dp),
    extraLarge = RoundedCornerShape(24.dp),
)

/**
 * Map the Pebble palette onto Material3's [androidx.compose.material3.ColorScheme]
 * slots. Lossy by design — M3 only exposes ~20 named roles, and a
 * few Pebble distinctions (chip palette, accent2 / accent3,
 * inkSoft vs inkMuted) don't have first-class spots. The mapping
 * here only needs to keep default-themed M3 widgets on-brand;
 * Pebble-native composables pull from [LocalPebbleColors] for the
 * full surface.
 *
 * Notable choices:
 * - `background` and `surface` both go to cream rather than the
 *   pure-white card surface. M3 widgets default-use `surface`; the
 *   cream reads as "studio paper" everywhere except explicit card
 *   composables that re-pin to `pebble.surface`.
 * - `secondary` = plum accent2; `tertiary` = sage accent3. Keeps
 *   the design's full accent triad available to any M3 widget that
 *   takes secondary/tertiary slots.
 * - `error` = danger; no override needed.
 * - Always-light scheme. The design is single-mode (warm cream)
 *   and the web side ships no dark variant either.
 */
private val PebbleColorScheme = lightColorScheme(
    primary = PebbleColors.Accent,
    onPrimary = PebbleColors.Surface,
    primaryContainer = PebbleColors.Chip1,
    onPrimaryContainer = PebbleColors.Ink,
    secondary = PebbleColors.Accent2,
    onSecondary = PebbleColors.Surface,
    tertiary = PebbleColors.Accent3,
    onTertiary = PebbleColors.Surface,
    background = PebbleColors.Bg,
    onBackground = PebbleColors.Ink,
    surface = PebbleColors.Bg,
    onSurface = PebbleColors.Ink,
    surfaceVariant = PebbleColors.BgAlt,
    onSurfaceVariant = PebbleColors.InkSoft,
    outline = PebbleColors.RuleStrong,
    outlineVariant = PebbleColors.Rule,
    error = PebbleColors.Danger,
    onError = PebbleColors.Surface,
)

/**
 * Root theme for every Pebble Compose tree. Wrap your activity's
 * `setContent { ... }` body in this. Provides:
 *  - Pebble Material3 color scheme + typography + shapes.
 *  - [LocalPebbleColors] / [LocalPebbleMonoFamily] for Pebble-
 *    native composables that need the full surface or the mono
 *    family directly.
 *
 * PR-A defines the theme but does not yet attach it to any
 * activity — `MainActivity` still drives XML layouts. PR-B is the
 * first to call `setContent { PebbleTheme { ... } }`.
 */
@Composable
fun PebbleTheme(content: @Composable () -> Unit) {
    CompositionLocalProvider(
        LocalPebbleColors provides PebbleStudioPalette,
        LocalPebbleMonoFamily provides GeistMono,
    ) {
        MaterialTheme(
            colorScheme = PebbleColorScheme,
            typography = PebbleTypography,
            shapes = PebbleShapes,
            content = content,
        )
    }
}

/**
 * `MaterialTheme.pebble` — extension accessor so composables read
 * Pebble-native tokens through the same path they read M3 tokens
 * (`MaterialTheme.colorScheme.primary`, `MaterialTheme.pebble.accent2`),
 * keeping call sites uniform.
 */
val MaterialTheme.pebble: PebblePalette
    @Composable
    @ReadOnlyComposable
    get() = LocalPebbleColors.current
