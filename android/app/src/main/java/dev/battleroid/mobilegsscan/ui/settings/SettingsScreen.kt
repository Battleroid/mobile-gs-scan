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
import androidx.compose.material3.Switch
import androidx.compose.material3.SwitchDefaults
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
    onCameraConfigKeyChange: (String) -> Unit,
    onCaptureProfileChange: (String) -> Unit,
    onFrameFilterEnabledChange: (Boolean) -> Unit,
    onFrameFilterBlurChange: (Int) -> Unit,
    onFrameFilterMotionChange: (Int) -> Unit,
    onFrameFilterExposureChange: (Int) -> Unit,
    onSaveClick: () -> Unit,
    onBackClick: () -> Unit,
    onProfileClick: () -> Unit,
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
        Header(onBackClick = onBackClick, onProfileClick = onProfileClick)

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
                title = "Capture profile",
                hint = "Smooth keeps every captured frame; Balanced and " +
                    "Sparse let the on-device quality filter drop blurry / " +
                    "shaky / overexposed frames so the studio trains on " +
                    "cleaner data. Pick Custom to mix and match below.",
            ) {
                CaptureProfileRow(
                    selected = state.captureProfile,
                    onChange = onCaptureProfileChange,
                )
            }

            Section(
                eyebrow = "camera",
                title = "Capture format",
                hint = "pick a resolution + frame rate your device's " +
                    "ARCore camera actually supports, or stay on " +
                    "Custom to keep the freeform fps slider below.",
            ) {
                CameraConfigRow(
                    selected = state.cameraConfigKey,
                    configs = state.cameraConfigs,
                    status = state.cameraProbeStatus,
                    onSelect = onCameraConfigKeyChange,
                )
            }

            // Freeform fps slider — only meaningful when the user
            // has the Custom preset selected. Hidden once they pick
            // a fixed (resolution × fps) preset because the ARCore
            // CameraConfig there pins the frame rate at the hardware
            // level and the slider becomes a footgun (it'd silently
            // request more frames than the camera delivers).
            if (state.cameraConfigKey == ServerConfig.CAMERA_CONFIG_CUSTOM) {
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
                    "for high. pick Custom to enter your own iteration count.",
            ) {
                TrainingPresetRow(
                    selected = state.trainIters,
                    onChange = onTrainItersChange,
                )
                if (isCustomTrainIters(state.trainIters)) {
                    Spacer(Modifier.height(8.dp))
                    CustomTrainItersInput(
                        value = state.trainIters,
                        onValueChange = onTrainItersChange,
                    )
                }
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

            // Frame-quality filter — bundled under "advanced" so
            // most users only see the Capture profile chips
            // above. The master switch hides the threshold sliders
            // when off so the section collapses to a single
            // toggle. Tweaking these is rarely necessary; the
            // canonical use case is the user has noticed a
            // specific failure mode (banded shadows, heavy hand
            // motion in a tight space) that the profile presets
            // don't handle well and wants to nudge.
            Section(
                eyebrow = "advanced",
                title = "Frame-quality filter",
                hint = "drops blurry / shaky / overexposed frames before " +
                    "they reach the studio. usually you'll set strictness " +
                    "via the Capture profile chips above; tweak here only " +
                    "if you have a specific failure mode to target.",
            ) {
                FilterToggleRow(
                    enabled = state.frameFilterEnabled,
                    onChange = onFrameFilterEnabledChange,
                )
                if (state.frameFilterEnabled) {
                    Spacer(Modifier.height(12.dp))
                    Text(
                        text = "Sharpness floor",
                        style = MaterialTheme.typography.labelSmall,
                        color = pebble.inkMuted,
                    )
                    IntSliderRow(
                        value = state.frameFilterBlur,
                        valueRange = ServerConfig.MIN_FRAME_FILTER_BLUR..
                            ServerConfig.MAX_FRAME_FILTER_BLUR,
                        onValueChange = onFrameFilterBlurChange,
                        suffix = "",
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        text = "Motion strictness",
                        style = MaterialTheme.typography.labelSmall,
                        color = pebble.inkMuted,
                    )
                    IntSliderRow(
                        value = state.frameFilterMotion,
                        valueRange = ServerConfig.MIN_FRAME_FILTER_MOTION..
                            ServerConfig.MAX_FRAME_FILTER_MOTION,
                        onValueChange = onFrameFilterMotionChange,
                        suffix = "%",
                    )
                    Spacer(Modifier.height(8.dp))
                    Text(
                        text = "Exposure strictness",
                        style = MaterialTheme.typography.labelSmall,
                        color = pebble.inkMuted,
                    )
                    IntSliderRow(
                        value = state.frameFilterExposure,
                        valueRange = ServerConfig.MIN_FRAME_FILTER_EXPOSURE..
                            ServerConfig.MAX_FRAME_FILTER_EXPOSURE,
                        onValueChange = onFrameFilterExposureChange,
                        suffix = "%",
                    )
                }
            }

            Spacer(Modifier.height(8.dp))
            SaveButton(onClick = onSaveClick)
            Spacer(Modifier.height(20.dp))
            AboutBlock()
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
private fun AboutBlock() {
    val pebble = MaterialTheme.pebble
    // Renders the build label the APK was actually packaged with.
    // Lived in the home header until the chip pushed the Settings
    // gear off the right edge on narrow phones; Settings is the
    // natural home for build metadata anyway (the user only really
    // needs to read it when filing a bug). ``BuildConfig.VERSION_NAME``
    // is the canonical value — bakes in ``+sha`` for dev builds and
    // a clean version for tagged releases (CI passes APP_BUILD_LABEL
    // to override the suffix).
    val rawVersion = dev.battleroid.mobilegsscan.BuildConfig.VERSION_NAME
    val label = "v" + rawVersion.replaceFirst('+', ' ').let { withSpace ->
        val parts = withSpace.split(' ', limit = 2)
        if (parts.size == 2 && parts[1].isNotBlank()) {
            "${parts[0]} · ${parts[1]}"
        } else {
            parts[0]
        }
    }
    Column(Modifier.fillMaxWidth()) {
        Text(
            text = "ABOUT",
            style = MaterialTheme.typography.labelSmall,
            color = pebble.inkMuted,
        )
        Spacer(Modifier.height(6.dp))
        Text(
            text = label,
            style = MaterialTheme.typography.bodyMedium,
            color = pebble.ink,
        )
    }
}

@Composable
private fun Header(onBackClick: () -> Unit, onProfileClick: () -> Unit) {
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
            modifier = Modifier.weight(1f),
        )
        // Profile entry. Sits opposite the back arrow on the right
        // — Settings is the discoverable hop into Profile this PR;
        // the design source doesn't surface a profile button on
        // the home header, so this is the natural slot.
        Box(
            modifier = Modifier
                .size(36.dp)
                .clip(androidx.compose.foundation.shape.CircleShape)
                .background(pebble.chip2)
                .border(
                    width = 1.dp,
                    color = pebble.rule,
                    shape = androidx.compose.foundation.shape.CircleShape,
                )
                .clickable(onClick = onProfileClick),
            contentAlignment = Alignment.Center,
        ) {
            // "MW" placeholder mirrors the design's profile avatar
            // initials. Replaced with real initials when auth ships.
            Text(
                text = "MW",
                style = MaterialTheme.typography.labelMedium.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
            )
        }
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
private fun CameraConfigRow(
    selected: String,
    configs: List<dev.battleroid.mobilegsscan.CameraConfigOption>,
    status: CameraProbeStatus,
    onSelect: (String) -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        when (status) {
            CameraProbeStatus.Pending -> {
                Text(
                    text = "probing supported formats…",
                    style = MaterialTheme.typography.bodySmall,
                    color = pebble.inkSoft,
                )
            }
            is CameraProbeStatus.Failed -> {
                // Soft-fail: keep the Custom chip available so a user
                // who hits Settings before granting camera permission
                // can still pick freeform fps. Surface the reason
                // inline so they know why the resolution chips aren't
                // there yet.
                Text(
                    text = status.reason,
                    style = MaterialTheme.typography.bodySmall,
                    color = pebble.warn,
                )
            }
            CameraProbeStatus.Ok -> Unit
        }

        // Chip flow: each device-supported preset followed by the
        // Custom chip. Wrapped via Column-of-Rows so a long list on
        // a wide preset matrix wraps gracefully without needing
        // FlowRow (which is still experimental in foundation).
        val chipsPerRow = 2
        val withCustom = configs + listOf(CUSTOM_OPTION)
        withCustom.chunked(chipsPerRow).forEach { row ->
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                row.forEach { opt ->
                    PresetCell(
                        modifier = Modifier.weight(1f),
                        label = opt.label.substringBefore(" · "),
                        sub = opt.label.substringAfter(" · ", missingDelimiterValue = ""),
                        selected = selected == opt.key,
                        onClick = { onSelect(opt.key) },
                    )
                }
                // Pad the final row so the lone trailing chip
                // (typical when the preset count is odd) keeps the
                // same width as its peers instead of stretching to
                // fill the row.
                if (row.size < chipsPerRow) {
                    Spacer(Modifier.weight((chipsPerRow - row.size).toFloat()))
                }
            }
        }
    }
}

/** Sentinel chip that always renders, so the user has an out from
 *  the device's preset matrix even when the probe failed or
 *  returned nothing useful. The freeform fps slider below the
 *  ``CameraConfigRow`` section only renders when this chip is
 *  selected. */
private val CUSTOM_OPTION = dev.battleroid.mobilegsscan.CameraConfigOption(
    key = dev.battleroid.mobilegsscan.ServerConfig.CAMERA_CONFIG_CUSTOM,
    label = "Custom · freeform fps",
    width = 0,
    height = 0,
    fps = 0,
)

@Composable
private fun TrainingPresetRow(
    selected: Int,
    onChange: (Int) -> Unit,
) {
    // 2×2 grid of preset chips so the four cells (Low / Standard /
    // High / Custom) stay readable on a phone-width screen. A single
    // row of four made the inner labels too cramped.
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
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
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            PresetCell(
                modifier = Modifier.weight(1f),
                label = "High",
                sub = "30 k iters",
                selected = selected == ServerConfig.TRAIN_ITERS_HIGH,
                onClick = { onChange(ServerConfig.TRAIN_ITERS_HIGH) },
            )
            PresetCell(
                modifier = Modifier.weight(1f),
                label = "Custom",
                // Sub label flips when Custom is active so the user
                // sees their current number echoed back. The
                // OutlinedTextField below the row is the actual
                // editor.
                sub = if (isCustomTrainIters(selected)) {
                    "$selected iters"
                } else {
                    "tune it"
                },
                selected = isCustomTrainIters(selected),
                onClick = {
                    // Seed the custom field with the current value
                    // when the user first taps Custom. If the value
                    // happens to be one of the three preset values
                    // (which is the case before Custom is ever
                    // tapped), bump it by 1 so the field renders
                    // "non-preset" and stays editable without
                    // immediately snapping back to the matching
                    // preset cell on the next composition.
                    val seed = if (isCustomTrainIters(selected)) {
                        selected
                    } else {
                        selected + 1
                    }
                    onChange(seed)
                },
            )
        }
    }
}

/**
 * True iff the iters value isn't one of the three named presets.
 * Used both to compute the "Custom" cell's selected highlight and
 * to drive the conditional reveal of the integer input below the
 * preset row.
 */
private fun isCustomTrainIters(iters: Int): Boolean =
    iters != ServerConfig.TRAIN_ITERS_LOW &&
        iters != ServerConfig.TRAIN_ITERS_STANDARD &&
        iters != ServerConfig.TRAIN_ITERS_HIGH

@Composable
private fun CustomTrainItersInput(
    value: Int,
    onValueChange: (Int) -> Unit,
) {
    val pebble = MaterialTheme.pebble
    // Mirror the textfield as a String so the user can hold a
    // transiently-empty field while typing (e.g. clearing to
    // re-enter). Pushed back to the Int state on every parse-able
    // change; we clamp to >= 1 to match ServerConfig's storage
    // contract.
    var text by remember(value) { mutableStateOf(value.toString()) }
    OutlinedTextField(
        value = text,
        onValueChange = { raw ->
            text = raw
            // Strip every non-digit before parsing so the displayed
            // grouping (`12 000`, `12,000`) parses cleanly into
            // 12000. The placeholder advertises grouped input, and
            // the soft keyboard's Number layout still surfaces a
            // space key that users naturally reach for at thousands
            // breaks. Anchoring on `isDigit()` rather than stripping
            // a specific separator covers locales that use comma /
            // dot / nbsp without us having to enumerate them.
            raw.filter { it.isDigit() }
                .toIntOrNull()
                ?.let { parsed -> onValueChange(parsed.coerceAtLeast(1)) }
        },
        label = { Text("Custom iteration count") },
        placeholder = { Text("e.g. 12 000") },
        singleLine = true,
        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
            keyboardType = androidx.compose.ui.text.input.KeyboardType.Number,
        ),
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
                cameraConfigKey = ServerConfig.CAMERA_CONFIG_CUSTOM,
                cameraConfigs = emptyList(),
                cameraProbeStatus = CameraProbeStatus.Ok,
                captureProfile = ServerConfig.CAPTURE_PROFILE_BALANCED,
                frameFilterEnabled = true,
                frameFilterBlur = ServerConfig.DEFAULT_FRAME_FILTER_BLUR,
                frameFilterMotion = ServerConfig.DEFAULT_FRAME_FILTER_MOTION,
                frameFilterExposure = ServerConfig.DEFAULT_FRAME_FILTER_EXPOSURE,
            ),
            onStudioUrlChange = {},
            onCaptureFpsChange = {},
            onJpegQualityChange = {},
            onTrainItersChange = {},
            onOverlayAlphaPctChange = {},
            onCameraConfigKeyChange = {},
            onCaptureProfileChange = {},
            onFrameFilterEnabledChange = {},
            onFrameFilterBlurChange = {},
            onFrameFilterMotionChange = {},
            onFrameFilterExposureChange = {},
            onSaveClick = {},
            onBackClick = {},
            onProfileClick = {},
        )
    }
}

/**
 * One-tap capture-profile chip row. Selecting a profile is
 * intentionally *just* a marker — the activity translates that
 * marker into the underlying fps + filter values when it commits
 * the form (see ``ServerConfigActivity.applyProfile``). Custom
 * means "leave the underlying sliders alone."
 */
@Composable
private fun CaptureProfileRow(
    selected: String,
    onChange: (String) -> Unit,
) {
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            PresetCell(
                modifier = Modifier.weight(1f),
                label = "Smooth",
                sub = "30 fps, lenient",
                selected = selected == ServerConfig.CAPTURE_PROFILE_SMOOTH,
                onClick = { onChange(ServerConfig.CAPTURE_PROFILE_SMOOTH) },
            )
            PresetCell(
                modifier = Modifier.weight(1f),
                label = "Balanced",
                sub = "15 fps, default",
                selected = selected == ServerConfig.CAPTURE_PROFILE_BALANCED,
                onClick = { onChange(ServerConfig.CAPTURE_PROFILE_BALANCED) },
            )
        }
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            PresetCell(
                modifier = Modifier.weight(1f),
                label = "Sparse",
                sub = "8 fps, strict",
                selected = selected == ServerConfig.CAPTURE_PROFILE_SPARSE,
                onClick = { onChange(ServerConfig.CAPTURE_PROFILE_SPARSE) },
            )
            PresetCell(
                modifier = Modifier.weight(1f),
                label = "Custom",
                sub = "use sliders",
                selected = selected == ServerConfig.CAPTURE_PROFILE_CUSTOM,
                onClick = { onChange(ServerConfig.CAPTURE_PROFILE_CUSTOM) },
            )
        }
    }
}

@Composable
private fun FilterToggleRow(
    enabled: Boolean,
    onChange: (Boolean) -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(
            text = if (enabled) "Filter is on" else "Filter is off",
            style = MaterialTheme.typography.labelLarge,
            color = pebble.ink,
        )
        Switch(
            checked = enabled,
            onCheckedChange = onChange,
            colors = SwitchDefaults.colors(
                checkedThumbColor = pebble.accent,
                checkedTrackColor = pebble.accent.copy(alpha = 0.35f),
                uncheckedThumbColor = pebble.inkMuted,
                uncheckedTrackColor = pebble.rule,
            ),
        )
    }
}
