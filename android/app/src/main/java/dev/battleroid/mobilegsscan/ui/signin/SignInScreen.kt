package dev.battleroid.mobilegsscan.ui.signin

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble

/**
 * Sign-in / Pair-to-Studio screen — placeholder.
 *
 * Phase 1's plan deliberately scoped real auth out: this screen
 * exists so the Profile flow has a place to land users on Sign
 * out, and so a future first-launch flow has a hook. Mirrors the
 * camera-feed look of `studio.jsx:1588` (`StudioAndroidSignIn`)
 * but **does not actually start a QR scanner** — we don't ship a
 * QR-decoding dependency. The reticle visuals are a no-op stand-
 * in; the only working path is the "Enter Studio URL instead"
 * CTA, which routes to ServerConfigActivity.
 *
 * When real pairing lands (QR + studio handshake), this screen's
 * static reticle becomes a live camera view; the rest of the
 * chrome can stay.
 */
@Composable
fun SignInScreen(
    onCloseClick: () -> Unit,
    onEnterUrlClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    // Dark camera-style chrome — even though the rest of the app
    // is light-mode. This screen models the QR-scanner viewfinder
    // the user will eventually point at the studio's web sign-in
    // page; black backdrop sells the "viewfinder" affordance.
    val backdropDark = Color(0xFF0E0B09)
    val ink = Color(0xFFF4ECDF)
    val inkMuted = ink.copy(alpha = 0.65f)

    Box(
        modifier = modifier
            .fillMaxSize()
            .background(backdropDark),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .windowInsetsPadding(
                    WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal)
                )
                // safeDrawing.Top = max(statusBars, displayCutout) —
                // the camera-style chrome is full-bleed dark so we
                // need cutout coverage, not just status-bar height,
                // or the top icon chips slide under a notch.
                .windowInsetsPadding(
                    WindowInsets.safeDrawing.only(WindowInsetsSides.Top)
                ),
        ) {
            TopBar(
                ink = ink,
                inkMuted = inkMuted,
                accent3 = pebble.accent3,
                onCloseClick = onCloseClick,
            )

            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 22.dp, vertical = 20.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    text = "★ PAIR TO STUDIO",
                    style = MaterialTheme.typography.labelSmall,
                    color = pebble.accent,
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    text = "Point at the QR on your computer",
                    style = MaterialTheme.typography.displaySmall.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = ink,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    text = "Open pebble.local in your browser to find it.",
                    style = MaterialTheme.typography.labelMedium,
                    color = inkMuted,
                )
            }

            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxWidth(),
                contentAlignment = Alignment.Center,
            ) {
                Reticle(accent = pebble.accent, hint = inkMuted)
            }

            BottomActions(
                ink = ink,
                inkMuted = inkMuted,
                accent = pebble.accent,
                onEnterUrlClick = onEnterUrlClick,
                modifier = Modifier
                    .windowInsetsPadding(
                        WindowInsets.systemBars.only(WindowInsetsSides.Bottom)
                    )
                    .padding(horizontal = 22.dp, vertical = 22.dp),
            )
        }
    }
}

@Composable
private fun TopBar(
    ink: Color,
    inkMuted: Color,
    accent3: Color,
    onCloseClick: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 18.dp, vertical = 14.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        IconChip(label = "✕", ink = ink, onClick = onCloseClick)
        Row(
            modifier = Modifier
                .clip(RoundedCornerShape(999.dp))
                .background(Color.Black.copy(alpha = 0.45f))
                .padding(horizontal = 12.dp, vertical = 7.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(7.dp)
                    .clip(CircleShape)
                    .background(accent3),
            )
            Spacer(Modifier.size(7.dp))
            Text(
                text = "searching…",
                style = MaterialTheme.typography.labelSmall,
                color = inkMuted,
            )
        }
        // Right slot would carry a torch toggle on a real scanner;
        // kept as a static visual hook so the layout matches the
        // design and the slot exists when we wire it.
        IconChip(label = "⚡", ink = ink, onClick = {})
    }
}

@Composable
private fun IconChip(label: String, ink: Color, onClick: () -> Unit) {
    Box(
        modifier = Modifier
            .size(36.dp)
            .clip(CircleShape)
            .background(Color.Black.copy(alpha = 0.45f))
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Text(label, color = ink, style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
private fun Reticle(accent: Color, hint: Color) {
    Box(
        modifier = Modifier.size(240.dp),
        contentAlignment = Alignment.Center,
    ) {
        // Four L-shaped corner brackets, drawn into a single Canvas
        // so we don't have to compose 4 sub-views just for the
        // visual hint. Stroke length matches the design's 38 unit
        // bracket; cap = Round so the inside of the L doesn't show
        // a square corner pixel.
        Canvas(modifier = Modifier.fillMaxSize()) {
            val stroke = 3.dp.toPx()
            val arm = 38.dp.toPx()
            val s = Stroke(width = stroke, cap = StrokeCap.Round)
            val w = size.width
            val h = size.height

            // top-left
            drawLine(accent, Offset(0f, 0f), Offset(arm, 0f), stroke, StrokeCap.Round)
            drawLine(accent, Offset(0f, 0f), Offset(0f, arm), stroke, StrokeCap.Round)
            // top-right
            drawLine(accent, Offset(w - arm, 0f), Offset(w, 0f), stroke, StrokeCap.Round)
            drawLine(accent, Offset(w, 0f), Offset(w, arm), stroke, StrokeCap.Round)
            // bottom-left
            drawLine(accent, Offset(0f, h - arm), Offset(0f, h), stroke, StrokeCap.Round)
            drawLine(accent, Offset(0f, h), Offset(arm, h), stroke, StrokeCap.Round)
            // bottom-right
            drawLine(accent, Offset(w - arm, h), Offset(w, h), stroke, StrokeCap.Round)
            drawLine(accent, Offset(w, h - arm), Offset(w, h), stroke, StrokeCap.Round)
        }
        Text(
            text = "ALIGN QR INSIDE FRAME",
            style = MaterialTheme.typography.labelSmall,
            color = hint,
        )
    }
}

@Composable
private fun BottomActions(
    ink: Color,
    inkMuted: Color,
    accent: Color,
    onEnterUrlClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier) {
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(12.dp))
                .background(Color.White.copy(alpha = 0.08f))
                .border(
                    width = 1.dp,
                    color = Color.White.copy(alpha = 0.14f),
                    shape = RoundedCornerShape(12.dp),
                )
                .clickable(onClick = onEnterUrlClick)
                .padding(vertical = 14.dp),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = "Enter Studio URL instead",
                style = MaterialTheme.typography.titleMedium.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = ink,
            )
        }
        Spacer(Modifier.height(10.dp))
        Text(
            text = "first time? open pebble.local on your laptop · " +
                "or paste your studio URL above",
            style = MaterialTheme.typography.labelSmall,
            color = inkMuted,
            modifier = Modifier
                .fillMaxWidth(),
        )
    }
}

@androidx.compose.ui.tooling.preview.Preview(
    showBackground = true,
    widthDp = 360,
    heightDp = 800,
)
@Composable
private fun SignInScreenPreview() {
    PebbleTheme {
        SignInScreen(
            onCloseClick = {},
            onEnterUrlClick = {},
        )
    }
}
