@file:OptIn(ExperimentalTextApi::class)

package dev.battleroid.mobilegsscan.ui.theme

import androidx.compose.material3.Typography
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.text.ExperimentalTextApi
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontVariation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp
import dev.battleroid.mobilegsscan.R

/**
 * Geist + Geist Mono — bundled as variable TTFs at `res/font/`.
 * Variable fonts collapse the weight axis into a single file, so
 * one TTF per family covers Regular/Medium/Semibold/Bold without
 * the static-weight per-file dance.
 *
 * Variable-font weight selection on Android needs a per-Font
 * [FontVariation] axis. Compose's [FontFamily] won't auto-pick
 * weights from a single variable file otherwise — every
 * `FontWeight.X` you reference resolves back to the same
 * default-weight glyphs unless an explicit `Font(...)` declares
 * `weight = FontWeight.X` plus the matching `wght` variation.
 * Build one entry per design weight we actually use; missing
 * weights resolve to the closest declared neighbour, which is
 * Material's standard fallback path.
 */
private fun geistVariable(weight: FontWeight): Font = Font(
    resId = R.font.geist,
    weight = weight,
    style = FontStyle.Normal,
    variationSettings = FontVariation.Settings(
        FontVariation.weight(weight.weight),
    ),
)

private fun geistMonoVariable(weight: FontWeight): Font = Font(
    resId = R.font.geist_mono,
    weight = weight,
    style = FontStyle.Normal,
    variationSettings = FontVariation.Settings(
        FontVariation.weight(weight.weight),
    ),
)

val GeistSans: FontFamily = FontFamily(
    geistVariable(FontWeight.Normal),
    geistVariable(FontWeight.Medium),
    geistVariable(FontWeight.SemiBold),
    geistVariable(FontWeight.Bold),
)

val GeistMono: FontFamily = FontFamily(
    geistMonoVariable(FontWeight.Normal),
    geistMonoVariable(FontWeight.Medium),
)

/**
 * Pebble type scale — maps Material3's 13 named text styles onto
 * the design's two families. Sans drives display + body; Mono is
 * reserved for technical chips (frame counts, scene IDs, kicker
 * captions). Sizes mirror the web side's CSS scale (15px base,
 * display weights at 28/22/18 sp).
 *
 * Letter-spacing in Material3 is expressed in `em` (proportional)
 * to keep the tightening consistent across sizes; the negative
 * tracking on display sizes matches the `-0.01em` the design
 * applies to the page H1.
 */
val PebbleTypography: Typography = Typography(
    displayLarge = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.SemiBold,
        fontSize = 36.sp,
        lineHeight = 44.sp,
        letterSpacing = (-0.02).em,
    ),
    displayMedium = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.SemiBold,
        fontSize = 28.sp,
        lineHeight = 34.sp,
        letterSpacing = (-0.015).em,
    ),
    displaySmall = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.SemiBold,
        fontSize = 22.sp,
        lineHeight = 28.sp,
        letterSpacing = (-0.01).em,
    ),
    headlineMedium = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.SemiBold,
        fontSize = 18.sp,
        lineHeight = 24.sp,
    ),
    titleLarge = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.SemiBold,
        fontSize = 16.sp,
        lineHeight = 22.sp,
    ),
    titleMedium = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.Medium,
        fontSize = 15.sp,
        lineHeight = 20.sp,
    ),
    bodyLarge = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.Normal,
        fontSize = 15.sp,
        lineHeight = 22.sp,
    ),
    bodyMedium = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.Normal,
        fontSize = 14.sp,
        lineHeight = 20.sp,
    ),
    bodySmall = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.Normal,
        fontSize = 12.sp,
        lineHeight = 16.sp,
    ),
    labelLarge = TextStyle(
        fontFamily = GeistSans,
        fontWeight = FontWeight.Medium,
        fontSize = 14.sp,
        lineHeight = 20.sp,
    ),
    // Small-caps mono chips (frame counts, timestamps, "v1.0 · LAN"
    // kicker style on the web side) ride labelMedium / labelSmall.
    // Generous tracking matches the design's uppercase rendering.
    labelMedium = TextStyle(
        fontFamily = GeistMono,
        fontWeight = FontWeight.Medium,
        fontSize = 11.sp,
        lineHeight = 14.sp,
        letterSpacing = 0.08.em,
    ),
    labelSmall = TextStyle(
        fontFamily = GeistMono,
        fontWeight = FontWeight.Medium,
        fontSize = 10.sp,
        lineHeight = 12.sp,
        letterSpacing = 0.1.em,
    ),
)

/**
 * Exposes the mono family separately from the Material3 typography
 * slot map. Several Pebble-native composables (job log tail,
 * frame-count overlay, ID chips) want mono regardless of size.
 * Using a CompositionLocal keeps those composables theme-clean —
 * they call `MaterialTheme.pebble.mono` rather than reaching into
 * the global [GeistMono] singleton, leaving room for a future
 * theme swap (e.g. dual-font support, accessibility large-text).
 */
val LocalPebbleMonoFamily = staticCompositionLocalOf { GeistMono }
