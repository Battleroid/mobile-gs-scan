package dev.battleroid.mobilegsscan.ui.settings

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
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import dev.battleroid.mobilegsscan.ServerConfig
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble

/**
 * Settings screen — Compose port of the programmatic
 * [dev.battleroid.mobilegsscan.ServerConfigActivity] form. Same
 * five fields the legacy screen surfaced: studio URL, capture
 * FPS, JPEG quality, training-fidelity preset, coverage-overlay
 * opacity. No new fields this PR — the design's capture-format
 * preset matrix + custom-resolution panel touch the
 * [ServerConfig] API surface, which is out of scope for the
 * Compose chrome rebuild.
 *
 * Mirrors `studio.jsx:1054` (`StudioAndroidSettings`) for the
 * visual treatment (cream paper, white surface cards per section,
 * mono small-caps "eyebrow" + display title + dust hint, tomato
 * save CTA), minus the unsupported fields.
 *
 * Renders state from [state]; emits user changes through the
 * `onXChange` callbacks so the activity can hold the
 * [SettingsUiState] in a `StateFlow` (single source of truth)
 * and commit on Save.
 */
@Composable
fun SettingsScreen(
    state: SettingsUiState,
    onStudioUrlChange: (String) -> Unit,
    onCaptureFpsChange: (Int) -> Unit,
    onJpegQualityChange: (Int) -> Unit,
    onTrainItersChange: (Int) -> Unit,
    onOverlayAlphaPctChange: (Int) -> Unit,
    onSaveClick: () -> Unit,
    onBackClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    val scroll = rememberScrollState()

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(pebble.bg)
            .windowInsetsPadding(
                WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal)
            )
            .windowInsetsPadding(
                WindowInsets.statusBars.only(WindowInsetsSides.Top)
            ),
    ) {
        Header(onBackClick = onBackClick)

        Column(
            modifier = Modifier
                .fillMaxWidth()
                .verticalScroll(scroll)
                .padding(horizontal = 16.dp)
                .padding(top = 8.dp),
            verticalArrangement = Arrangement.spacedBy(18.dp),
        ) {
            Section(
                eyebrow = "studio",
                title = "Studio URL",
                hint = "scheme optional — we'll prepend https:// for you. " +
                    "type http:// explicitly if your studio runs without TLS.",
            ) {
                OutlinedTextField(
                    value = state.studioUrl,
                    onValueChange = onStudioUrlChange,
                    placeholder = { Text("192.168.1.42 or studio.local") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                    modifier = Modifier.fillMaxWidth(),
                    colors = OutlinedTextFieldDefaults.colors(
                        focusedBorderColor = pebble.accent,
                        unfocusedBorderColor = pebble.rule,
                        cursorColor = pebble.accent,
                        focusedContainerColor = pebble.surface,
                        unfocusedContainerColor = pebble.surface,
                    ),
                )
            }

            Section(
                eyebrow = "camera",
                title = "Capture rate",
                hint = "frames per second sent to the studio. higher = " +
                    "smoother coverage but more bandwidth and battery " +
                    "drain. 10 fps is the default.",
            ) {
                IntSliderRow(
                    value = state.captureFps,
                    valueRange = ServerConfig.MIN_FPS..ServerConfig.MAX_FPS,
                    onValueChange = onCaptureFpsChange,
                    suffix = "fps",
                )
            }

            Section(
                eyebrow = "camera",
                title = "JPEG quality",
                hint = "image quality of streamed frames. higher = sharper " +
                    "input for splatting but more bandwidth per frame. " +
                    "85% is the default.",
            ) {
                IntSliderRow(
                    value = state.jpegQuality,
                    valueRange = ServerConfig.MIN_JPEG_QUALITY..ServerConfig.MAX_JPEG_QUALITY,
                    onValueChange = onJpegQualityChange,
                    suffix = "%",
                )
            }

            Section(
                eyebrow = "capture quality",
                title = "Training fidelity",
                hint = "how long the splatfacto trainer runs per capture. " +
                    "higher = sharper splats but longer wait. on a 4090 " +
                    "expect ~3 min for low, ~10 min for standard, ~25 min " +
                    "for high.",
            ) {
                TrainingPresetRow(
                    selected = state.trainIters,
                    onChange = onTrainItersChange,
                )
            }

            Section(
                eyebrow = "HUD",
                title = "Coverage overlay opacity",
                hint = "how solid the AR coverage dots are. lower lets " +
                    "more of the camera image show through. 70% is the " +
                    "default.",
            ) {
                IntSliderRow(
                    value = state.overlayAlphaPct,
                    valueRange = ServerConfig.MIN_OVERLAY_ALPHA_PCT..
                        ServerConfig.MAX_OVERLAY_ALPHA_PCT,
                    onValueChange = onOverlayAlphaPctChange,
                    suffix = "%",
                )
            }

            Spacer(Modifier.height(8.dp))
            SaveButton(onClick = onSaveClick)
            // Footer space so the Save button can scroll off the
            // gesture nav cleanly on short forms.
            Spacer(
                Modifier
                    .windowInsetsPadding(
                        WindowInsets.systemBars.only(WindowInsetsSides.Bottom)
                    )
                    .height(24.dp),
            )
        }
    }
}

@Composable
private fun Header(onBackClick: () -> Unit) {
    val pebble = MaterialTheme.pebble
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 16.dp, end = 16.dp, top = 10.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(36.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(pebble.surface)
                .border(
                    width = 1.dp,
                    color = pebble.rule,
                    shape = RoundedCornerShape(12.dp),
                )
                .clickable(onClick = onBackClick),
            contentAlignment = Alignment.Center,
        ) {
            Text("←", style = MaterialTheme.typography.bodyLarge, color = pebble.ink)
        }
        Spacer(Modifier.size(12.dp))
        Text(
            text = "Settings",
            style = MaterialTheme.typography.displaySmall.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.ink,
        )
    }
}

@Composable
private fun Section(
    eyebrow: String,
    title: String,
    hint: String,
    content: @Composable () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Column {
        Text(
            text = eyebrow.uppercase(),
            style = MaterialTheme.typography.labelSmall,
            color = pebble.inkMuted,
        )
        Spacer(Modifier.height(2.dp))
        Text(
            text = title,
            style = MaterialTheme.typography.titleLarge.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.ink,
        )
        Spacer(Modifier.height(4.dp))
        Text(
            text = hint,
            style = MaterialTheme.typography.bodyMedium,
            color = pebble.inkSoft,
        )
        Spacer(Modifier.height(10.dp))
        content()
    }
}

@Composable
private fun IntSliderRow(
    value: Int,
    valueRange: IntRange,
    onValueChange: (Int) -> Unit,
    suffix: String,
) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .padding(horizontal = 16.dp, vertical = 12.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.Bottom,
        ) {
            Text(
                text = "${valueRange.first} $suffix",
                style = MaterialTheme.typography.labelSmall,
                color = pebble.inkMuted,
            )
            Text(
                text = "$value $suffix",
                style = MaterialTheme.typography.titleMedium.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
            )
            Text(
                text = "${valueRange.last} $suffix",
                style = MaterialTheme.typography.labelSmall,
                color = pebble.inkMuted,
            )
        }
        Slider(
            value = value.toFloat(),
            onValueChange = { onValueChange(it.toInt()) },
            valueRange = valueRange.first.toFloat()..valueRange.last.toFloat(),
            // steps = (range-1) snaps the thumb to integer positions.
            // valueRange is inclusive on both sides; Slider's `steps`
            // is the count of intermediate stops, exclusive of the
            // endpoints. range.last - range.first - 1 gives that.
            steps = (valueRange.last - valueRange.first - 1).coerceAtLeast(0),
            colors = SliderDefaults.colors(
                thumbColor = pebble.accent,
                activeTrackColor = pebble.accent,
                inactiveTrackColor = pebble.rule,
            ),
        )
    }
}

@Composable
private fun TrainingPresetRow(
    selected: Int,
    onChange: (Int) -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        PresetCell(
            modifier = Modifier.weight(1f),
            label = "Low",
            sub = "5 k iters",
            selected = selected == ServerConfig.TRAIN_ITERS_LOW,
            onClick = { onChange(ServerConfig.TRAIN_ITERS_LOW) },
        )
        PresetCell(
            modifier = Modifier.weight(1f),
            label = "Standard",
            sub = "15 k iters",
            selected = selected == ServerConfig.TRAIN_ITERS_STANDARD,
            onClick = { onChange(ServerConfig.TRAIN_ITERS_STANDARD) },
        )
        PresetCell(
            modifier = Modifier.weight(1f),
            label = "High",
            sub = "30 k iters",
            selected = selected == ServerConfig.TRAIN_ITERS_HIGH,
            onClick = { onChange(ServerConfig.TRAIN_ITERS_HIGH) },
        )
    }
}

@Composable
private fun PresetCell(
    label: String,
    sub: String,
    selected: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    val borderColor = if (selected) pebble.accent else pebble.rule
    val tintColor = if (selected) pebble.accent.copy(alpha = 0.08f) else pebble.surface
    Column(
        modifier = modifier
            .clip(RoundedCornerShape(12.dp))
            .background(tintColor)
            .border(
                width = if (selected) 1.5.dp else 1.dp,
                color = borderColor,
                shape = RoundedCornerShape(12.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 12.dp),
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodyLarge.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.ink,
        )
        Spacer(Modifier.height(2.dp))
        Text(
            text = sub,
            style = MaterialTheme.typography.labelSmall,
            color = pebble.inkMuted,
        )
    }
}

@Composable
private fun SaveButton(onClick: () -> Unit) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(18.dp))
            .background(pebble.accent)
            .clickable(onClick = onClick)
            .padding(vertical = 16.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = "Save",
            style = MaterialTheme.typography.titleLarge.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = Color.White,
        )
    }
}

@androidx.compose.ui.tooling.preview.Preview(
    showBackground = true,
    widthDp = 360,
    heightDp = 800,
)
@Composable
private fun SettingsScreenPreview() {
    PebbleTheme {
        SettingsScreen(
            state = SettingsUiState(
                studioUrl = "https://studio.local:5443",
                captureFps = 10,
                jpegQuality = 85,
                trainIters = ServerConfig.TRAIN_ITERS_STANDARD,
                overlayAlphaPct = 70,
            ),
            onStudioUrlChange = {},
            onCaptureFpsChange = {},
            onJpegQualityChange = {},
            onTrainItersChange = {},
            onOverlayAlphaPctChange = {},
            onSaveClick = {},
            onBackClick = {},
        )
    }
}
