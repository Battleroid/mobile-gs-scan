package dev.battleroid.mobilegsscan.ui.detail

import coil.compose.AsyncImage
import androidx.compose.ui.layout.ContentScale
import dev.battleroid.mobilegsscan.ui.draft.formatHumanBytes
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.battleroid.mobilegsscan.StudioClient
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble

/**
 * Capture detail screen — Compose port of the legacy XML-driven
 * `CaptureDetailActivity`. Renders the polled capture row + (when
 * the scene exists) the pipeline jobs list. Mirrors
 * `studio.jsx:806` (`StudioAndroidDetail`) for the visual shape:
 * cream paper, back-arrow header with status kicker, display-h1
 * title with rename gesture, hero gradient block, mono-eyebrow
 * pipeline section, "Open viewer in browser" bottom CTA.
 *
 * Behaviour preserved verbatim from the legacy activity:
 *  - Tap capture name → rename dialog (optimistic local update;
 *    next poll re-renders from the server).
 *  - Tap a job row → routes to JobDetailActivity via `onJobClick`.
 *  - "Open viewer in browser" routes to the studio web UI via
 *    `onOpenViewerClick`. CTA is hidden until an artifact URL
 *    appears on the scene.
 *  - Back routes through `onBackClick`.
 *
 * Deliberately omitted vs the design source: the per-frame
 * thumbnail grid inside the hero block. Phase 1's `Scene.thumb_url`
 * is a single PNG; building a per-frame mosaic needs new server
 * work. Hero stays a chip-palette gradient placeholder consistent
 * with the home grid until that arrives.
 */
@Composable
fun CaptureDetailScreen(
    state: CaptureDetailUiState,
    onBackClick: () -> Unit,
    onRenameSubmit: (String) -> Unit,
    onJobClick: (StudioClient.JobView) -> Unit,
    onOpenViewerClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    val scroll = rememberScrollState()
    var renameDialog: CaptureRenameDialog? by remember { mutableStateOf(null) }
    val capture = state.capture
    val scene = state.scene
    // Computed once so the scroll tail-spacer can omit its bottom
    // system-bar inset when the CTA is rendered (the CTA already
    // applies that inset itself, double-padding would push it up).
    val artifactReady =
        !scene?.ply_url.isNullOrBlank() || !scene?.spz_url.isNullOrBlank()

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
        DetailHeader(
            status = capture?.status,
            networkError = state.networkError,
            onBackClick = onBackClick,
        )

        Column(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(scroll)
                .padding(horizontal = 20.dp),
        ) {
            // Title block: display-h1 + tap-to-rename + mono kicker.
            TitleBlock(
                name = state.captureName.ifBlank { "loading…" },
                kicker = capture?.let { c ->
                    buildString {
                        append("${c.frame_count} frames")
                        val bytes = c.total_bytes ?: 0L
                        if (bytes > 0) {
                            append(" · ")
                            append(formatHumanBytes(bytes))
                        }
                        if (c.dropped_count > 0) append(" (${c.dropped_count} dropped)")
                        append(" · ${c.source}")
                        if (c.created_at.isNotBlank()) {
                            append(" · ")
                            append(c.created_at)
                        }
                    }
                } ?: "",
                onRenameTap = {
                    if (state.captureName.isNotBlank()) {
                        renameDialog = CaptureRenameDialog(initialName = state.captureName)
                    }
                },
            )
            Spacer(Modifier.height(16.dp))

            // Hero. Server-rendered PNG when available, falling back
            // to a chip-palette gradient matching the home grid's
            // thumbnail style. orbit_url playback is deferred — the
            // still PNG is the canonical thumbnail and lands first.
            //
            // Hero tap mirrors the bottom CTA: tap → open the splat
            // viewer when an artefact is ready, no-op otherwise.
            // The hero is the most prominent surface on the screen,
            // so users naturally reach for it before scrolling to
            // the CTA — wiring both paths to the same handler keeps
            // discovery cheap.
            HeroBlock(
                paletteSeed = capture?.id ?: "loading",
                frameCount = capture?.frame_count ?: 0,
                thumbUrl = dev.battleroid.mobilegsscan.ui.home.absoluteUrl(
                    state.baseUrl, scene?.thumb_url,
                ),
                onClick = if (artifactReady) onOpenViewerClick else null,
            )

            if (!capture?.error.isNullOrBlank()) {
                Spacer(Modifier.height(16.dp))
                ErrorCard(error = capture.error.orEmpty())
            }

            Spacer(Modifier.height(20.dp))

            // Pipeline section.
            Text(
                text = "PIPELINE · TAP A STEP TO INSPECT",
                style = MaterialTheme.typography.labelSmall,
                color = pebble.inkMuted,
                modifier = Modifier.padding(bottom = 8.dp),
            )

            when {
                state.sceneMissing || scene == null -> {
                    SceneMissingCard()
                }
                else -> {
                    Column(
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        scene.jobs.forEach { job ->
                            MiniJobRow(job = job, onClick = { onJobClick(job) })
                        }
                    }
                }
            }

            Spacer(Modifier.height(16.dp))

            // Inset-aware tail padding. The bottom CTA below applies
            // its own systemBars bottom inset, but that only runs in
            // the artifact-ready branch — the common case
            // (`training` / `queued` captures with no artifact yet)
            // would otherwise drop the last pipeline card under the
            // gesture nav. Apply the inset unconditionally inside
            // the scroll content so the bottom card stays readable
            // whether or not the CTA renders.
            Spacer(
                Modifier
                    .windowInsetsPadding(
                        WindowInsets.systemBars.only(WindowInsetsSides.Bottom)
                    )
                    .height(if (artifactReady) 0.dp else 16.dp),
            )
        }

        // Bottom CTA — only render when an artifact is available.
        if (artifactReady) {
            OpenViewerButton(
                onClick = onOpenViewerClick,
                modifier = Modifier
                    .windowInsetsPadding(
                        WindowInsets.systemBars.only(WindowInsetsSides.Bottom)
                    )
                    .padding(horizontal = 20.dp, vertical = 16.dp),
            )
        }
    }

    renameDialog?.let { dialog ->
        RenameDialog(
            initialName = dialog.initialName,
            onSubmit = { newName ->
                renameDialog = null
                if (newName.isNotBlank() && newName != dialog.initialName) {
                    onRenameSubmit(newName)
                }
            },
            onDismiss = { renameDialog = null },
        )
    }
}

@Composable
private fun DetailHeader(
    status: String?,
    networkError: String?,
    onBackClick: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 4.dp),
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
        Spacer(Modifier.size(14.dp))

        // Status kicker — mono dot + status text. Falls back to
        // networkError when present (transient connectivity issue).
        if (networkError != null) {
            StatusKicker(label = "offline · $networkError", tone = pebble.danger)
        } else if (status != null) {
            val tone = when (status) {
                "completed" -> pebble.accent3
                "failed", "canceled" -> pebble.danger
                "processing", "queued", "uploading", "created" -> pebble.accent
                else -> pebble.inkMuted
            }
            StatusKicker(label = displayStatus(status), tone = tone)
        } else {
            StatusKicker(label = "loading…", tone = pebble.inkMuted)
        }
    }
}

@Composable
private fun StatusKicker(label: String, tone: Color) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(
            modifier = Modifier
                .size(6.dp)
                .clip(CircleShape)
                .background(tone),
        )
        Spacer(Modifier.size(6.dp))
        Text(
            text = label,
            style = MaterialTheme.typography.labelMedium,
            color = tone,
        )
    }
}

private fun displayStatus(s: String): String = when (s) {
    "completed" -> "ready"
    "processing", "queued", "uploading", "created" -> "training"
    else -> s
}

@Composable
private fun TitleBlock(
    name: String,
    kicker: String,
    onRenameTap: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .padding(top = 4.dp)
            .clickable(onClick = onRenameTap),
    ) {
        Text(
            text = name,
            style = MaterialTheme.typography.displayMedium.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.ink,
        )
        if (kicker.isNotBlank()) {
            Spacer(Modifier.height(6.dp))
            Text(
                text = kicker,
                style = MaterialTheme.typography.labelMedium,
                color = pebble.inkSoft,
            )
        }
    }
}

@Composable
private fun HeroBlock(
    paletteSeed: String,
    frameCount: Int,
    thumbUrl: String?,
    onClick: (() -> Unit)? = null,
) {
    val pebble = MaterialTheme.pebble
    val palette = paletteFor(paletteSeed, pebble)
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(220.dp)
            .clip(RoundedCornerShape(18.dp))
            .background(Brush.linearGradient(palette))
            // Tap gated on ``onClick`` being non-null (the caller
            // passes null when no artefact is ready yet, e.g. the
            // splat is still training). Without the gate a tap on
            // the gradient placeholder would surface a confusing
            // no-op or a broken-link toast.
            .then(
                if (onClick != null) Modifier.clickable(onClick = onClick)
                else Modifier
            ),
    ) {
        if (thumbUrl != null) {
            AsyncImage(
                model = thumbUrl,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        }
        // Top-left frame-count chip
        Row(
            modifier = Modifier
                .padding(12.dp)
                .clip(RoundedCornerShape(999.dp))
                .background(Color.Black.copy(alpha = 0.55f))
                .padding(horizontal = 10.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(6.dp)
                    .clip(CircleShape)
                    .background(pebble.accent),
            )
            Spacer(Modifier.size(6.dp))
            Text(
                text = "frames · $frameCount",
                style = MaterialTheme.typography.labelSmall,
                color = Color.White,
            )
        }
    }
}

@Composable
private fun ErrorCard(error: String) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.danger.copy(alpha = 0.08f))
            .border(1.dp, pebble.danger.copy(alpha = 0.4f), RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        Text(
            text = "ERROR",
            style = MaterialTheme.typography.labelSmall,
            color = pebble.danger,
        )
        Spacer(Modifier.height(4.dp))
        Text(
            text = error,
            style = MaterialTheme.typography.bodyMedium,
            color = pebble.ink,
        )
    }
}

@Composable
private fun SceneMissingCard() {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .padding(horizontal = 16.dp, vertical = 16.dp),
    ) {
        Text(
            text = "capture not finalized yet — pipeline jobs will appear here once " +
                "the capture is submitted for processing",
            style = MaterialTheme.typography.bodyMedium,
            color = pebble.inkSoft,
        )
    }
}

@Composable
internal fun MiniJobRow(
    job: StudioClient.JobView,
    onClick: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    val tone = when (job.status) {
        "completed" -> pebble.accent3
        "running", "claimed" -> pebble.accent
        "failed", "canceled" -> pebble.danger
        else -> pebble.inkMuted
    }
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .clickable(onClick = onClick)
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    modifier = Modifier
                        .size(7.dp)
                        .clip(CircleShape)
                        .background(tone),
                )
                Spacer(Modifier.size(8.dp))
                Text(
                    text = job.kind,
                    style = MaterialTheme.typography.labelMedium.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = pebble.ink,
                )
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = job.status.uppercase(),
                    style = MaterialTheme.typography.labelSmall,
                    color = pebble.inkSoft,
                )
                Spacer(Modifier.size(6.dp))
                Text(
                    text = "›",
                    style = MaterialTheme.typography.bodyLarge,
                    color = pebble.inkMuted,
                )
            }
        }
        if (!job.progress_msg.isNullOrBlank()) {
            Spacer(Modifier.height(4.dp))
            Text(
                text = job.progress_msg,
                style = MaterialTheme.typography.labelSmall,
                color = pebble.inkSoft,
                modifier = Modifier.padding(start = 15.dp),
            )
        }
        Spacer(Modifier.height(8.dp))
        // Slim progress bar at the bottom of the row. Pinned to 100%
        // for completed/canceled so the visual reads "done" rather
        // than "stuck mid-train" when the job lands without an
        // explicit progress=1.0 update.
        val visualPct = when (job.status) {
            "completed", "canceled" -> 1f
            else -> job.progress.coerceIn(0f, 1f)
        }
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(4.dp)
                .clip(RoundedCornerShape(999.dp))
                .background(pebble.rule),
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth(visualPct)
                    .height(4.dp)
                    .clip(RoundedCornerShape(999.dp))
                    .background(tone),
            )
        }
    }
}

@Composable
private fun OpenViewerButton(onClick: () -> Unit, modifier: Modifier = Modifier) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(18.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(18.dp))
            .clickable(onClick = onClick)
            .padding(vertical = 14.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = "Open viewer in browser ↗",
            style = MaterialTheme.typography.titleMedium.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.ink,
        )
    }
}

@Composable
private fun RenameDialog(
    initialName: String,
    onSubmit: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    var value by remember { mutableStateOf(initialName) }
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(onClick = { onSubmit(value.trim()) }) { Text("Save") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        },
        title = { Text("Rename capture") },
        text = {
            OutlinedTextField(
                value = value,
                onValueChange = { value = it },
                placeholder = { Text("capture name") },
                singleLine = true,
                colors = OutlinedTextFieldDefaults.colors(
                    focusedBorderColor = pebble.accent,
                    cursorColor = pebble.accent,
                ),
            )
        },
    )
}

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

@androidx.compose.ui.tooling.preview.Preview(
    showBackground = true,
    widthDp = 360,
    heightDp = 800,
)
@Composable
private fun CaptureDetailScreenPreview() {
    PebbleTheme {
        CaptureDetailScreen(
            state = CaptureDetailUiState(
                captureName = "studio plant — fig",
                capture = StudioClient.Capture(
                    id = "cap1",
                    name = "studio plant — fig",
                    status = "processing",
                    source = "phone",
                    frame_count = 286,
                    dropped_count = 0,
                    has_pose = true,
                    scene_id = "scene1",
                    error = null,
                    created_at = "12 min ago",
                    updated_at = "12 min ago",
                ),
                scene = StudioClient.Scene(
                    id = "scene1",
                    capture_id = "cap1",
                    status = "processing",
                    error = null,
                    ply_url = null,
                    spz_url = null,
                    jobs = listOf(
                        StudioClient.JobView(
                            id = "j1", kind = "glomap-sfm", status = "completed",
                            progress = 1f, progress_msg = "cameras recovered · 1m 48s",
                        ),
                        StudioClient.JobView(
                            id = "j2", kind = "splatfacto", status = "running",
                            progress = 0.62f,
                            progress_msg = "iter 9 300 · loss 0.0183 · ~6 min left",
                        ),
                        StudioClient.JobView(
                            id = "j3", kind = "export-spz", status = "queued",
                            progress = 0f, progress_msg = "waiting",
                        ),
                    ),
                    created_at = "12 min ago",
                ),
                sceneMissing = false,
                networkError = null,
                renaming = false,
            ),
            onBackClick = {},
            onRenameSubmit = {},
            onJobClick = {},
            onOpenViewerClick = {},
        )
    }
}
