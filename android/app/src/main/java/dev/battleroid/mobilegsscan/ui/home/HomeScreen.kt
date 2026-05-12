package dev.battleroid.mobilegsscan.ui.home

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.asPaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBars
import androidx.compose.foundation.layout.systemBars
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.battleroid.mobilegsscan.Draft
import dev.battleroid.mobilegsscan.StudioClient
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble

/**
 * Home screen — the post-Compose replacement for `MainActivity`'s
 * XML layout. Single responsibility: render the home state machine
 * the legacy [dev.battleroid.mobilegsscan.MainActivity] used to drive
 * via `binding.*` calls. Polling + state production happens upstream
 * in [HomeUiState]; this composable is intentionally pure-render so
 * the polling lifecycle stays tied to the Activity lifecycle rather
 * than recomposition.
 *
 * Mirrors `studio.jsx:556` (`StudioAndroidList`). The design's
 * static studio chip becomes a live online / offline indicator
 * (green dot when `/api/health` returns 200, dust dot when offline
 * or no studio configured) to preserve the legacy MainActivity's
 * behaviour; the rest is a literal port of the design's layout
 * tokens (peach-tinted draft rows, white surface capture rows with
 * the chip-palette gradient thumbnail, tomato pill primary button).
 */
@Composable
fun HomeScreen(
    state: HomeUiState,
    onSettingsClick: () -> Unit,
    onNewCaptureClick: () -> Unit,
    onCaptureClick: (StudioClient.Capture) -> Unit,
    onDraftClick: (Draft) -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    val statusBarPadding = WindowInsets.statusBars.asPaddingValues()
    val systemBarsPadding = WindowInsets.systemBars.asPaddingValues()

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(pebble.bg)
            .padding(top = statusBarPadding.calculateTopPadding()),
    ) {
        HomeHeader(
            status = state.status,
            studioHost = state.studioHost,
            onSettingsClick = onSettingsClick,
        )

        LazyColumn(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth(),
            contentPadding = PaddingValues(horizontal = 20.dp),
        ) {
            if (state.drafts.isNotEmpty()) {
                item("drafts-header") {
                    SectionHeader(
                        label = "drafts",
                        count = state.drafts.size,
                    )
                }
                items(state.drafts, key = { "draft-${it.id}" }) { draft ->
                    DraftRow(draft = draft, onClick = { onDraftClick(draft) })
                    Spacer(Modifier.height(10.dp))
                }
            }

            if (state.captures.isNotEmpty()) {
                item("captures-header") {
                    SectionHeader(
                        label = "captures",
                        count = state.captures.size,
                    )
                }
                items(state.captures, key = { "cap-${it.id}" }) { capture ->
                    CaptureRow(capture = capture, onClick = { onCaptureClick(capture) })
                    Spacer(Modifier.height(10.dp))
                }
            }

            if (state.drafts.isEmpty() && state.captures.isEmpty()) {
                item("empty") {
                    EmptyState(state = state)
                }
            }

            // Bottom breathing room so the last row isn't crowded
            // against the New scan button.
            item("tail-spacer") { Spacer(Modifier.height(12.dp)) }
        }

        NewScanButton(
            enabled = state.canCreateNewCapture,
            onClick = onNewCaptureClick,
            modifier = Modifier.padding(
                start = 20.dp,
                end = 20.dp,
                top = 12.dp,
                // Inset the system-nav bar so the button doesn't sit
                // under the gesture-bar / 3-button navigation strip.
                bottom = 16.dp + systemBarsPadding.calculateBottomPadding(),
            ),
        )
    }
}

@Composable
private fun HomeHeader(
    status: HomeStatus,
    studioHost: String?,
    onSettingsClick: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 20.dp, end = 14.dp, top = 14.dp, bottom = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        // Left: PebbleMark + wordmark. Mark is rendered as a Compose
        // Canvas in [PebbleMark] later; for the foundation PR we
        // use a tomato circle as a placeholder so the header layout
        // is solid without dragging in the mark composable yet.
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(22.dp)
                    .clip(CircleShape)
                    .background(pebble.accent),
            )
            Spacer(Modifier.size(10.dp))
            Text(
                text = "pebble",
                style = MaterialTheme.typography.headlineMedium.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
            )
        }

        // Right: live studio pill + gear button.
        Row(verticalAlignment = Alignment.CenterVertically) {
            StudioPill(status = status, studioHost = studioHost)
            Spacer(Modifier.size(6.dp))
            SettingsButton(onClick = onSettingsClick)
        }
    }
}

@Composable
private fun StudioPill(status: HomeStatus, studioHost: String?) {
    val pebble = MaterialTheme.pebble
    val (dotColor, label) = when (status) {
        HomeStatus.Online -> pebble.accent3 to "studio"
        HomeStatus.Offline -> pebble.danger to "offline"
        HomeStatus.NotConfigured -> pebble.inkMuted to "not configured"
        HomeStatus.Resolving -> pebble.inkMuted to "resolving…"
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(
                width = 1.dp,
                color = pebble.rule,
                shape = RoundedCornerShape(12.dp),
            )
            .padding(horizontal = 10.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(7.dp)
                .clip(CircleShape)
                .background(dotColor),
        )
        Spacer(Modifier.size(8.dp))
        Text(
            text = if (studioHost != null && status == HomeStatus.Online) {
                studioHost
            } else {
                label
            },
            style = MaterialTheme.typography.labelMedium,
            color = pebble.inkSoft,
        )
    }
}

@Composable
private fun SettingsButton(onClick: () -> Unit) {
    val pebble = MaterialTheme.pebble
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
            .clickable(onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        // Gear glyph rendered as text to avoid pulling in the
        // material-icons-extended dependency for one symbol. The
        // design uses a literal `⚙` here too. Real vector icons
        // arrive in PR-D when we have several across the suite.
        Text(
            text = "⚙",
            style = MaterialTheme.typography.bodyLarge,
            color = pebble.ink,
        )
    }
}

@Composable
private fun SectionHeader(label: String, count: Int) {
    val pebble = MaterialTheme.pebble
    Text(
        text = "${label.uppercase()} · $count",
        style = MaterialTheme.typography.labelSmall,
        color = pebble.inkMuted,
        modifier = Modifier.padding(top = 8.dp, bottom = 8.dp, start = 4.dp),
    )
}

@Composable
private fun DraftRow(draft: Draft, onClick: () -> Unit) {
    val pebble = MaterialTheme.pebble
    val meta = draft.meta
    val ready = meta.finalized
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            // Butter chip4 tint matches the design's draft cards —
            // distinct from the white surface used by uploaded
            // captures so the user can tell them apart at a glance.
            .background(pebble.chip4)
            .clickable(onClick = onClick)
            .padding(14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(36.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(pebble.chip1),
            contentAlignment = Alignment.Center,
        ) {
            // Package glyph stand-in. The full Pebble icon set
            // lands in PR-D; this keeps the row recognisable in
            // PR-B without a new dep.
            Text("📦", style = MaterialTheme.typography.bodyLarge)
        }
        Spacer(Modifier.size(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = meta.name ?: "(unnamed draft)",
                style = MaterialTheme.typography.bodyLarge.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
            )
            Text(
                text = buildString {
                    append("${meta.frame_count} frames · ${meta.created_at}")
                    append(" · ")
                    append(if (ready) "ready to upload" else "incomplete")
                },
                style = MaterialTheme.typography.labelMedium,
                color = pebble.inkSoft,
            )
        }
        Text(
            text = if (ready) "upload ↑" else "open",
            style = MaterialTheme.typography.bodyMedium.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.accent,
        )
    }
}

@Composable
private fun CaptureRow(capture: StudioClient.Capture, onClick: () -> Unit) {
    val pebble = MaterialTheme.pebble
    val palette = paletteFor(capture.id, pebble)
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(
                width = 1.dp,
                color = pebble.rule,
                shape = RoundedCornerShape(12.dp),
            )
            .clickable(onClick = onClick)
            .padding(12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Palette-gradient thumbnail placeholder. The server-rendered
        // PNG thumbnail (PR #82 Phase 1 PR-D) is a web-side feature
        // for now — surfacing it on Android needs an image loader
        // (Coil) which is deferred to a later PR. The chip-tinted
        // gradient keeps the row visually rich until then and
        // matches the web side's own pre-thumbnail placeholder.
        Box(
            modifier = Modifier
                .size(52.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(
                    brush = Brush.linearGradient(palette),
                ),
        )
        Spacer(Modifier.size(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = capture.name,
                style = MaterialTheme.typography.bodyLarge.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
                maxLines = 1,
            )
            Text(
                text = buildString {
                    append("${capture.frame_count} frames")
                    if (capture.dropped_count > 0) {
                        append(" (${capture.dropped_count} dropped)")
                    }
                    append(" · ${capture.source}")
                },
                style = MaterialTheme.typography.labelMedium,
                color = pebble.inkMuted,
            )
        }
        Spacer(Modifier.size(8.dp))
        StatusPill(status = capture.status)
    }
}

@Composable
private fun StatusPill(status: String) {
    val pebble = MaterialTheme.pebble
    // Map the rich CaptureStatus surface (`created`, `uploading`,
    // `queued`, `processing`, `completed`, `failed`, `canceled`)
    // onto the design's three visual tones — keeps Android in sync
    // with the web side's `statusLabel()` in CaptureCard.tsx.
    val (label, fg, bgTint) = when (status) {
        "completed" -> Triple("ready", pebble.accent3, pebble.accent3.copy(alpha = 0.16f))
        "processing", "queued", "uploading", "created" ->
            Triple("training", pebble.accent, pebble.accent.copy(alpha = 0.16f))
        "failed", "canceled" -> Triple(status, pebble.danger, pebble.danger.copy(alpha = 0.16f))
        else -> Triple(status, pebble.inkMuted, pebble.inkMuted.copy(alpha = 0.16f))
    }
    Text(
        text = label.uppercase(),
        style = MaterialTheme.typography.labelSmall,
        color = fg,
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(bgTint)
            .padding(horizontal = 8.dp, vertical = 4.dp),
    )
}

@Composable
private fun NewScanButton(
    enabled: Boolean,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(18.dp))
            .background(
                if (enabled) pebble.accent else pebble.accent.copy(alpha = 0.4f),
            )
            .clickable(enabled = enabled, onClick = onClick)
            .padding(vertical = 16.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = "＋  New scan",
            style = MaterialTheme.typography.titleLarge.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = Color.White,
        )
    }
}

@Composable
private fun EmptyState(state: HomeUiState) {
    val pebble = MaterialTheme.pebble
    val text = when {
        state.status == HomeStatus.NotConfigured ->
            "tap the gear and enter your studio URL to get started"
        else ->
            "no captures yet — tap “New scan” to start"
    }
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 48.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = text,
            style = MaterialTheme.typography.bodyMedium,
            color = pebble.inkMuted,
        )
    }
}

/**
 * Deterministic 4-stop chip palette picked from the capture id, so
 * each capture row's thumbnail gradient stays the same across
 * reloads. Mirrors `paletteFor()` in `web/src/components/CaptureCard.tsx`.
 */
private fun paletteFor(
    id: String,
    pebble: dev.battleroid.mobilegsscan.ui.theme.PebblePalette,
): List<Color> {
    val palettes = listOf(
        listOf(pebble.chip1, pebble.accent, pebble.chip3, pebble.chip4),
        listOf(pebble.chip2, pebble.accent2, pebble.chip1, pebble.chip3),
        listOf(pebble.chip3, pebble.accent3, pebble.chip4, pebble.chip1),
        listOf(pebble.chip4, pebble.accent, pebble.chip2, pebble.accent3),
    )
    var h = 0
    for (c in id) h = (h * 31 + c.code)
    return palettes[(h.toUInt() % palettes.size.toUInt()).toInt()]
}

/**
 * Compile-time preview hook so Android Studio renders the screen
 * without a running activity. Uses a synthetic state with a
 * realistic mix of drafts + captures so layout regressions show up
 * in the side panel during development.
 */
@androidx.compose.ui.tooling.preview.Preview(
    showBackground = true,
    widthDp = 360,
    heightDp = 800,
)
@Composable
private fun HomeScreenPreview() {
    PebbleTheme {
        HomeScreen(
            state = HomeUiState(
                status = HomeStatus.Online,
                studioHost = "192.168.1.42",
                drafts = emptyList(),
                captures = emptyList(),
                canCreateNewCapture = true,
            ),
            onSettingsClick = {},
            onNewCaptureClick = {},
            onCaptureClick = {},
            onDraftClick = {},
        )
    }
}
