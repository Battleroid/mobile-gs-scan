package dev.battleroid.mobilegsscan.ui.capture

import android.content.Context
import android.opengl.GLSurfaceView
import androidx.compose.foundation.background
import androidx.compose.foundation.border
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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble

/**
 * AR capture screen — Compose chrome layered over the ARCore /
 * camera GLSurfaceView. The activity owns the GLSurfaceView and
 * passes a factory in; Compose's [AndroidView] interop hosts it
 * underneath the HUD overlay.
 *
 * Mirrors `studio.jsx:645` (`StudioAndroidAR`):
 *  - Glass-pill top bar: capture name (left), live frame count
 *    with pulsing tomato dot (right).
 *  - Glass-pill coverage HUD: ring + percentage + total points
 *    (centre-top).
 *  - Big circular tomato CTA (centre-bottom): START before
 *    capture-gate flip, FINISH after.
 *  - Pre-start hint card: white surface card in the centre of the
 *    preview that disappears once the user taps START.
 *
 * Behaviour preserved verbatim from the legacy XML implementation —
 * no new interactions added in PR-C, this is a chrome rebuild.
 * The side `gear` and `cancel` icons in the design are intentionally
 * omitted: there's no current functional analog (Settings is reached
 * from Home; Cancel overlaps with the Finish dialog's "Discard"
 * choice). They'll get wired in when the design's intended behaviour
 * is finalised.
 */
@Composable
fun CaptureScreen(
    state: CaptureUiState,
    dialogs: CaptureDialogs,
    onStartClick: () -> Unit,
    onFinishClick: () -> Unit,
    onArUnsupportedConfirm: () -> Unit,
    onArUnsupportedDismiss: () -> Unit,
    onFinishUploadNow: () -> Unit,
    onFinishSaveLater: () -> Unit,
    onFinishDiscard: () -> Unit,
    glSurfaceFactory: (Context) -> GLSurfaceView,
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier
            .fillMaxSize()
            // Black underneath so the pre-render flash (between
            // setContent and the first GL frame) is camera-feed
            // dark rather than a flash of cream from PebbleTheme's
            // default surface.
            .background(Color.Black),
    ) {
        // ARCore + camera — owned by the activity, handed in here.
        // AndroidView's `factory` runs once per AndroidView instance;
        // we don't pass an `update` because the GLSurfaceView's own
        // onResume/onPause is driven by the activity lifecycle, not
        // recomposition.
        AndroidView(
            factory = glSurfaceFactory,
            modifier = Modifier.fillMaxSize(),
        )

        // HUD chrome layered on top. safeDrawing.only(Horizontal)
        // covers landscape nav bars + side cutouts; top + bottom are
        // handled per-row so the HUD pills sit just under the status
        // bar and the CTA clears the gesture bar.
        Box(
            modifier = Modifier
                .fillMaxSize()
                .windowInsetsPadding(
                    WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal)
                ),
        ) {
            TopBar(
                sessionName = state.sessionName,
                captureActive = state.captureActive,
                frameCount = state.frameCount,
                modifier = Modifier
                    .align(Alignment.TopStart)
                    .fillMaxWidth()
                    .windowInsetsPadding(
                        WindowInsets.statusBars.only(WindowInsetsSides.Top)
                    )
                    .padding(horizontal = 16.dp, vertical = 12.dp),
            )

            state.coverage?.let { coverage ->
                CoverageHud(
                    coverage = coverage,
                    modifier = Modifier
                        .align(Alignment.TopCenter)
                        .windowInsetsPadding(
                            WindowInsets.statusBars.only(WindowInsetsSides.Top)
                        )
                        // ~64 dp under the top bar so the two pills
                        // don't crowd each other on devices with a
                        // tall status bar.
                        .padding(top = 64.dp),
                )
            }

            if (!state.captureActive) {
                StartHintCard(
                    modifier = Modifier
                        .align(Alignment.Center)
                        .padding(horizontal = 24.dp),
                )
            }

            BottomCta(
                captureActive = state.captureActive,
                onStartClick = onStartClick,
                onFinishClick = onFinishClick,
                modifier = Modifier
                    .align(Alignment.BottomCenter)
                    // System bars bottom inset so the CTA clears the
                    // gesture / nav bar; an extra 24 dp on top so it
                    // sits visually away from the very bottom edge.
                    .windowInsetsPadding(
                        WindowInsets.systemBars.only(WindowInsetsSides.Bottom)
                    )
                    .padding(bottom = 24.dp),
            )
        }

        dialogs.arUnsupported?.let { d ->
            ArUnsupportedDialog(
                extra = d.extra,
                onConfirm = onArUnsupportedConfirm,
                onDismiss = onArUnsupportedDismiss,
            )
        }
        dialogs.finishPrompt?.let { d ->
            FinishPromptDialog(
                frameCount = d.frameCount,
                onUploadNow = onFinishUploadNow,
                onSaveLater = onFinishSaveLater,
                onDiscard = onFinishDiscard,
            )
        }
    }
}

@Composable
private fun TopBar(
    sessionName: String,
    captureActive: Boolean,
    frameCount: Int,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        GlassPill {
            Text(
                text = sessionName.ifBlank { "—" },
                style = MaterialTheme.typography.labelMedium,
                color = Color.White,
            )
        }
        GlassPill {
            Row(verticalAlignment = Alignment.CenterVertically) {
                // Live indicator dot. Tomato when actively recording,
                // dust grey when the capture gate is still closed —
                // mirrors the legacy "idle — tap Start" state.
                val pebble = MaterialTheme.pebble
                Box(
                    modifier = Modifier
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(if (captureActive) pebble.accent else pebble.inkMuted),
                )
                Spacer(Modifier.size(8.dp))
                Text(
                    text = if (captureActive) "$frameCount frames" else "idle",
                    style = MaterialTheme.typography.labelMedium,
                    color = Color.White,
                )
            }
        }
    }
}

@Composable
private fun CoverageHud(
    coverage: Coverage,
    modifier: Modifier = Modifier,
) {
    GlassPill(
        modifier = modifier,
        shape = RoundedCornerShape(999.dp),
        contentPadding = PadHelpers.coveragePillPadding,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            CoverageRing(pct = coverage.wellCoveredPct, size = 42.dp)
            Spacer(Modifier.size(12.dp))
            Column {
                Text(
                    text = "${coverage.wellCoveredPct}%",
                    style = MaterialTheme.typography.headlineMedium.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = Color.White,
                )
                Text(
                    text = "covered · ${coverage.totalPoints} pts",
                    style = MaterialTheme.typography.labelSmall,
                    color = Color.White.copy(alpha = 0.7f),
                )
            }
        }
    }
}

@Composable
private fun CoverageRing(
    pct: Int,
    size: Dp,
) {
    val pebble = MaterialTheme.pebble
    val sweep = (pct.coerceIn(0, 100) / 100f) * 360f
    Canvas(modifier = Modifier.size(size)) {
        val stroke = 4.dp.toPx()
        val pad = stroke / 2f
        val arcSize = Size(this.size.width - stroke, this.size.height - stroke)
        // Background track — translucent white, full circle.
        drawArc(
            color = Color.White.copy(alpha = 0.15f),
            startAngle = -90f,
            sweepAngle = 360f,
            useCenter = false,
            topLeft = Offset(pad, pad),
            size = arcSize,
            style = Stroke(width = stroke, cap = StrokeCap.Round),
        )
        // Foreground — sage accent3, sweep proportional to coverage.
        drawArc(
            color = pebble.accent3,
            startAngle = -90f,
            sweepAngle = sweep,
            useCenter = false,
            topLeft = Offset(pad, pad),
            size = arcSize,
            style = Stroke(width = stroke, cap = StrokeCap.Round),
        )
    }
}

@Composable
private fun StartHintCard(modifier: Modifier = Modifier) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = modifier
            .clip(RoundedCornerShape(18.dp))
            .background(Color.White.copy(alpha = 0.95f))
            .padding(horizontal = 18.dp, vertical = 14.dp),
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                text = "Aim, then tap START.",
                style = MaterialTheme.typography.bodyLarge.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
            )
            Spacer(Modifier.size(2.dp))
            Text(
                text = "we'll keep capturing until you tap finish.",
                style = MaterialTheme.typography.labelMedium,
                color = pebble.inkSoft,
            )
        }
    }
}

@Composable
private fun BottomCta(
    captureActive: Boolean,
    onStartClick: () -> Unit,
    onFinishClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    val label = if (captureActive) "FINISH" else "START"
    val onClick = if (captureActive) onFinishClick else onStartClick
    Box(
        modifier = modifier,
        contentAlignment = Alignment.Center,
    ) {
        // Glow ring — translucent tomato halo around the pill.
        // Drawn as a slightly larger Box behind the inner CTA so we
        // don't have to reach for a Canvas just for shadow.
        Box(
            modifier = Modifier
                .size(90.dp)
                .clip(CircleShape)
                .background(pebble.accent.copy(alpha = 0.3f)),
        )
        Box(
            modifier = Modifier
                .size(78.dp)
                .clip(CircleShape)
                .background(pebble.accent)
                .border(width = 4.dp, color = Color.White, shape = CircleShape)
                .clickable(onClick = onClick),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = label,
                style = MaterialTheme.typography.labelMedium.copy(
                    fontWeight = FontWeight.Bold,
                ),
                color = Color.White,
            )
        }
    }
}

/**
 * Translucent dark pill the design uses for camera-overlay HUD.
 * `backdrop-filter: blur(8px)` from the source isn't representable
 * cheaply in Compose without a BlurredBackground composable; the
 * 0.55 alpha black approximates the visual weight on a busy camera
 * feed and matches the legacy `#80000000` background used by the
 * coverage HUD and start hint TextViews.
 */
@Composable
private fun GlassPill(
    modifier: Modifier = Modifier,
    shape: androidx.compose.ui.graphics.Shape = RoundedCornerShape(12.dp),
    contentPadding: androidx.compose.foundation.layout.PaddingValues =
        PadHelpers.defaultGlassPillPadding,
    content: @Composable () -> Unit,
) {
    Box(
        modifier = modifier
            .clip(shape)
            .background(Color.Black.copy(alpha = 0.55f))
            .padding(contentPadding),
    ) {
        content()
    }
}

@Composable
private fun ArUnsupportedDialog(
    extra: String?,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(onClick = onConfirm) {
                Text("Open Play Store")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Cancel")
            }
        },
        title = { Text("ARCore reports unsupported") },
        text = {
            Column {
                Text(
                    text = "Your phone hardware should support ARCore, but " +
                        "the system component that gates it (\"Google Play " +
                        "Services for AR\") is reporting otherwise. This " +
                        "usually means it's missing or out of date.\n\n" +
                        "Tap \"Open Play Store\" to install or update it, " +
                        "then come back to this screen and try again.",
                )
                if (!extra.isNullOrBlank()) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        text = "($extra)",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        },
    )
}

@Composable
private fun FinishPromptDialog(
    frameCount: Int,
    onUploadNow: () -> Unit,
    onSaveLater: () -> Unit,
    onDiscard: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = { /* modal — must pick a path */ },
        confirmButton = {
            TextButton(onClick = onUploadNow) { Text("Upload now") }
        },
        dismissButton = {
            // M3 AlertDialog only has confirm + dismiss slots; pack
            // the third action ("Save for later") in here so all three
            // sit in the bottom row with the more conservative paths
            // on the left, dangerous ones on the right.
            Row {
                TextButton(onClick = onDiscard) {
                    Text("Discard", color = MaterialTheme.colorScheme.error)
                }
                TextButton(onClick = onSaveLater) { Text("Save for later") }
            }
        },
        title = { Text("Finish capture") },
        text = {
            Text(
                "Recorded $frameCount ${if (frameCount == 1) "frame" else "frames"}. " +
                    "Upload now, save the draft for later, or discard it?",
            )
        },
    )
}

private object PadHelpers {
    val defaultGlassPillPadding =
        androidx.compose.foundation.layout.PaddingValues(
            horizontal = 12.dp,
            vertical = 8.dp,
        )
    val coveragePillPadding =
        androidx.compose.foundation.layout.PaddingValues(
            horizontal = 16.dp,
            vertical = 10.dp,
        )
}

@androidx.compose.ui.tooling.preview.Preview(
    showBackground = true,
    widthDp = 360,
    heightDp = 800,
)
@Composable
private fun CaptureScreenPreview_Idle() {
    PebbleTheme {
        CaptureScreen(
            state = CaptureUiState(
                sessionName = "studio plant — fig",
                captureActive = false,
                frameCount = 0,
                coverage = null,
            ),
            dialogs = CaptureDialogs.None,
            onStartClick = {},
            onFinishClick = {},
            onArUnsupportedConfirm = {},
            onArUnsupportedDismiss = {},
            onFinishUploadNow = {},
            onFinishSaveLater = {},
            onFinishDiscard = {},
            // Preview-only stub — Compose previews don't run real
            // OpenGL, so the AndroidView's factory just returns a
            // bare GLSurfaceView the preview will render as a black
            // box. Production wiring goes through CaptureActivity.
            glSurfaceFactory = { ctx -> GLSurfaceView(ctx) },
        )
    }
}

@androidx.compose.ui.tooling.preview.Preview(
    showBackground = true,
    widthDp = 360,
    heightDp = 800,
)
@Composable
private fun CaptureScreenPreview_Recording() {
    PebbleTheme {
        CaptureScreen(
            state = CaptureUiState(
                sessionName = "studio plant — fig",
                captureActive = true,
                frameCount = 286,
                coverage = Coverage(wellCoveredPct = 62, totalPoints = 1824),
            ),
            dialogs = CaptureDialogs.None,
            onStartClick = {},
            onFinishClick = {},
            onArUnsupportedConfirm = {},
            onArUnsupportedDismiss = {},
            onFinishUploadNow = {},
            onFinishSaveLater = {},
            onFinishDiscard = {},
            glSurfaceFactory = { ctx -> GLSurfaceView(ctx) },
        )
    }
}
