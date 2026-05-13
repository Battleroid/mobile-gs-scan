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
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.asPaddingValues
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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import coil.request.ImageRequest
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
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
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(
    state: HomeUiState,
    isRefreshing: Boolean,
    onRefresh: () -> Unit,
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
            // Apply left + right safeDrawing insets at the root so
            // landscape navigation bars, display cutouts, and
            // foldables don't tuck rows / the bottom CTA under
            // system UI. The legacy XML home padded all four sides
            // via setOnApplyWindowInsetsListener; the Compose port
            // initially applied only the top inset, which regressed
            // edge-to-edge safety on phones held landscape and on
            // any device with a side cutout. safeDrawing is the
            // union of system bars + display cutout — the standard
            // "don't draw me where the user can't see / tap" inset.
            //
            // Top + bottom stay handled explicitly:
            //   * top via the calculateTopPadding() read below so
            //     the header sits below the status bar.
            //   * bottom is owned by NewScanButton's modifier.padding
            //     so the CTA clears the gesture bar.
            // Splitting horizontal vs vertical here keeps that
            // explicit handling intact.
            .windowInsetsPadding(
                WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal)
            )
            .padding(top = statusBarPadding.calculateTopPadding()),
    ) {
        HomeHeader(
            status = state.status,
            studioHost = state.studioHost,
            onSettingsClick = onSettingsClick,
        )

        // PullToRefreshBox wraps only the list region — the header
        // and bottom New-scan button stay anchored. Swipe pulls the
        // (cosmetic) indicator down inside this Box; the parent
        // activity owns isRefreshing and toggles it off after
        // pollOnce() + refreshDrafts() return.
        //
        // ``ExperimentalMaterial3Api`` opt-in is the standard
        // requirement for PullToRefreshBox in material3 1.3.x; the
        // composable itself is stable but lives behind the same
        // gate the rest of the M3 pull-to-refresh surface uses.
        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = onRefresh,
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth(),
        ) {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
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
                        CaptureRow(
                            capture = capture,
                            baseUrl = state.baseUrl,
                            onClick = { onCaptureClick(capture) },
                        )
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
        // Left: PebbleMark + wordmark + build-label chip. Mark is
        // rendered as a Compose Canvas in [PebbleMark] later; for the
        // foundation PR we use a tomato circle as a placeholder so
        // the header layout is solid without dragging in the mark
        // composable yet. The chip pins ``v<base>+<sha>`` from
        // BuildConfig so a user reporting a bug can read the exact
        // commit they're on without rummaging through Settings.
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
            Spacer(Modifier.size(10.dp))
            VersionChip()
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
private fun VersionChip() {
    val pebble = MaterialTheme.pebble
    // Read from BuildConfig (populated by build.gradle.kts at compile
    // time from the repo's VERSION file + git short-SHA). Falls back
    // to the bare base version when the SHA is empty (sandboxed
    // builds, tagged releases).
    val label = dev.battleroid.mobilegsscan.BuildConfig.APP_BASE_VERSION.let { base ->
        val sha = dev.battleroid.mobilegsscan.BuildConfig.APP_BUILD_SHA
        if (sha.isBlank()) "v$base" else "v$base · $sha"
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .border(
                width = 1.dp,
                color = pebble.rule,
                shape = RoundedCornerShape(999.dp),
            )
            .padding(horizontal = 8.dp, vertical = 2.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = pebble.inkMuted,
        )
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
    // First captured frame on disk, if any. Loaded via Coil so the
    // row shows what the user actually scanned rather than a generic
    // emoji glyph. ``thumbnailFile()`` returns the first JPEG under
    // the draft's frames/ dir (null for drafts created but never
    // captured into).
    val thumbFile = remember(draft.id, meta.frame_count) { draft.thumbnailFile() }
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
            if (thumbFile != null) {
                AsyncImage(
                    model = thumbFile,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            } else {
                Text("📦", style = MaterialTheme.typography.bodyLarge)
            }
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
private fun CaptureRow(
    capture: StudioClient.Capture,
    baseUrl: String?,
    onClick: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    val palette = paletteFor(capture.id, pebble)
    val thumbAbsUrl = remember(capture.thumb_url, baseUrl) {
        absoluteUrl(baseUrl, capture.thumb_url)
    }
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
        // Server-rendered PNG when present; falls back to a chip-
        // tinted gradient placeholder so the row stays visually
        // rich while the pipeline's thumbnail step is mid-render
        // (or skipped — stub captures, ns-render unavailable, etc.).
        Box(
            modifier = Modifier
                .size(52.dp)
                .clip(RoundedCornerShape(8.dp))
                .background(brush = Brush.linearGradient(palette)),
        ) {
            if (thumbAbsUrl != null) {
                AsyncImage(
                    model = ImageRequest.Builder(LocalContext.current)
                        .data(thumbAbsUrl)
                        .crossfade(true)
                        .build(),
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
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

/** Resolve a server-relative path (``/api/scenes/…``) against an
 *  optional base URL. Returns null when either side is missing so
 *  callers (Coil) skip the load and fall back to the placeholder
 *  rather than asking for a malformed URL. */
internal fun absoluteUrl(baseUrl: String?, relative: String?): String? {
    val b = baseUrl?.trimEnd('/') ?: return null
    val r = relative ?: return null
    return if (r.startsWith("http://") || r.startsWith("https://")) r
    else b + (if (r.startsWith("/")) r else "/$r")
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
            isRefreshing = false,
            onRefresh = {},
            onSettingsClick = {},
            onNewCaptureClick = {},
            onCaptureClick = {},
            onDraftClick = {},
        )
    }
}
