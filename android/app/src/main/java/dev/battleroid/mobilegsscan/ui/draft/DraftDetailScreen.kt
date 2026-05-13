package dev.battleroid.mobilegsscan.ui.draft

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
import androidx.compose.foundation.shape.RoundedCornerShape
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
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble

/**
 * Local-draft detail screen — Compose port of the legacy XML
 * `DraftDetailActivity`. Lists the draft's frame count + created
 * timestamp + finalization state, lets the user rename (the local
 * name carries over to the server name on upload), and offers
 * Upload Now / Discard. While an upload is in flight the action
 * row collapses into a progress bar.
 *
 * Mirrors `studio.jsx:740` (`StudioAndroidDraft`) for the visual
 * shape: cream paper, back-arrow header with mono "DRAFT" kicker,
 * display-h1 title with kicker line, hero gradient block, white
 * surface stats card (status / created / frames), bottom row with
 * tomato Upload CTA + small Discard glyph.
 *
 * Behaviour preserved verbatim from the legacy activity:
 *  - Rename dialog flips the local meta name; survives across
 *    activity restarts via DraftStore.setName.
 *  - Discard prompts for confirmation, deletes on accept.
 *  - Upload Now: shows progress bar, on success routes forward to
 *    CaptureDetail (activity owns this routing).
 *  - Auto-upload via EXTRA_AUTO_UPLOAD on launch (activity owns
 *    that trigger).
 */
@Composable
fun DraftDetailScreen(
    state: DraftDetailUiState,
    onBackClick: () -> Unit,
    onRenameSubmit: (String?) -> Unit,
    onUploadClick: () -> Unit,
    onCancelUploadClick: () -> Unit,
    onDiscardConfirmed: () -> Unit,
    onUploadErrorDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    var renameDialog by remember { mutableStateOf<String?>(null) }
    var discardConfirm by remember { mutableStateOf(false) }
    val meta = state.meta
    val uploadInProgress = state.upload != null

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
                .weight(1f)
                .fillMaxWidth()
                .padding(horizontal = 20.dp),
        ) {
            // Title block — display h1 + tap-to-rename + mono kicker.
            Column(
                modifier = Modifier
                    .padding(top = 4.dp)
                    .clickable(enabled = meta != null) {
                        meta?.let { renameDialog = it.name.orEmpty() }
                    },
            ) {
                Text(
                    text = meta?.name ?: "(unnamed draft)",
                    style = MaterialTheme.typography.displayMedium.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = pebble.ink,
                )
                if (meta != null) {
                    Spacer(Modifier.height(6.dp))
                    val sizeBlurb = formatHumanBytes(state.totalBytes)
                    Text(
                        text = buildString {
                            append("${meta.frame_count} frames")
                            if (sizeBlurb.isNotBlank()) append(" · $sizeBlurb")
                            append(" · captured ${meta.created_at}")
                        },
                        style = MaterialTheme.typography.labelMedium,
                        color = pebble.inkSoft,
                    )
                }
            }

            Spacer(Modifier.height(16.dp))

            // Hero gradient block. The legacy activity didn't have
            // one; Pebble's design source does. Static placeholder
            // (chip-palette) consistent with the home grid and
            // CaptureDetail.
            HeroBlock(seed = meta?.id ?: "loading")

            Spacer(Modifier.height(20.dp))

            // Stats card.
            if (meta != null) {
                StatsCard(meta = meta, totalBytes = state.totalBytes)
            }

            Spacer(Modifier.weight(1f))
        }

        BottomActions(
            upload = state.upload,
            canUpload = meta?.frame_count?.let { it > 0 } ?: false,
            onUploadClick = onUploadClick,
            onCancelUploadClick = onCancelUploadClick,
            onDiscardClick = { discardConfirm = true },
            uploadInProgress = uploadInProgress,
            modifier = Modifier
                .windowInsetsPadding(
                    WindowInsets.systemBars.only(WindowInsetsSides.Bottom)
                )
                .padding(horizontal = 20.dp, vertical = 16.dp),
        )
    }

    renameDialog?.let { initial ->
        RenameDialog(
            initial = initial,
            onSubmit = { trimmed ->
                renameDialog = null
                onRenameSubmit(trimmed.takeIf { it.isNotBlank() })
            },
            onDismiss = { renameDialog = null },
        )
    }

    if (discardConfirm) {
        AlertDialog(
            onDismissRequest = { discardConfirm = false },
            confirmButton = {
                TextButton(onClick = {
                    discardConfirm = false
                    onDiscardConfirmed()
                }) { Text("Discard", color = pebble.danger) }
            },
            dismissButton = {
                TextButton(onClick = { discardConfirm = false }) { Text("Cancel") }
            },
            title = { Text("Discard draft?") },
            text = {
                Text(
                    "All recorded frames will be deleted from the phone. " +
                        "This cannot be undone.",
                )
            },
        )
    }

    state.uploadError?.let { msg ->
        AlertDialog(
            onDismissRequest = onUploadErrorDismiss,
            confirmButton = {
                TextButton(onClick = onUploadErrorDismiss) { Text("Back") }
            },
            title = { Text("Upload failed") },
            text = { Text(msg) },
        )
    }
}

@Composable
private fun Header(onBackClick: () -> Unit) {
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
                .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
                .clickable(onClick = onBackClick),
            contentAlignment = Alignment.Center,
        ) {
            Text("←", style = MaterialTheme.typography.bodyLarge, color = pebble.ink)
        }
        Spacer(Modifier.size(14.dp))
        Text(
            text = "DRAFT",
            style = MaterialTheme.typography.labelMedium,
            color = pebble.inkMuted,
        )
    }
}

@Composable
private fun HeroBlock(seed: String) {
    val pebble = MaterialTheme.pebble
    val palette = paletteFor(seed, pebble)
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(200.dp)
            .clip(RoundedCornerShape(18.dp))
            .background(Brush.linearGradient(palette)),
    ) {
        Row(
            modifier = Modifier
                .padding(12.dp)
                .clip(RoundedCornerShape(999.dp))
                .background(pebble.ink.copy(alpha = 0.7f))
                .padding(horizontal = 10.dp, vertical = 4.dp),
        ) {
            Text(
                text = "preview · raw frames",
                style = MaterialTheme.typography.labelSmall,
                color = pebble.bg,
            )
        }
    }
}

@Composable
private fun StatsCard(meta: dev.battleroid.mobilegsscan.DraftMeta, totalBytes: Long) {
    val pebble = MaterialTheme.pebble
    val statusLabel = if (meta.finalized) "ready to upload" else "incomplete"
    val statusTint = if (meta.finalized) pebble.accent3 else pebble.warn
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp)),
    ) {
        StatRow(label = "status", value = statusLabel, tint = statusTint)
        Divider()
        StatRow(label = "created", value = meta.created_at)
        Divider()
        StatRow(label = "frames", value = meta.frame_count.toString())
        Divider()
        StatRow(
            label = "size",
            value = formatHumanBytes(totalBytes).ifBlank { "—" },
            last = true,
        )
    }
}

/**
 * Human-readable byte total — `28.4 MB`, `184 KB`, etc. Uses
 * binary units (MiB-as-MB) so the numbers line up with what the
 * file manager / settings storage breakdown report. Empty string
 * for zero so the title kicker can omit the "0 B" stub gracefully.
 */
internal fun formatHumanBytes(bytes: Long): String {
    if (bytes <= 0L) return ""
    val kib = bytes / 1024.0
    if (kib < 1.0) return "$bytes B"
    val mib = kib / 1024.0
    if (mib < 1.0) return "%.0f KB".format(kib)
    val gib = mib / 1024.0
    if (gib < 1.0) return "%.1f MB".format(mib)
    return "%.2f GB".format(gib)
}

@Composable
private fun StatRow(
    label: String,
    value: String,
    tint: Color? = null,
    last: Boolean = false,
) {
    val pebble = MaterialTheme.pebble
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 12.dp),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = label.uppercase(),
            style = MaterialTheme.typography.labelSmall,
            color = pebble.inkMuted,
        )
        Text(
            text = value,
            style = MaterialTheme.typography.labelMedium.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = tint ?: pebble.ink,
        )
    }
}

@Composable
private fun Divider() {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .height(1.dp)
            .background(pebble.rule),
    )
}

@Composable
private fun BottomActions(
    upload: UploadProgress?,
    canUpload: Boolean,
    uploadInProgress: Boolean,
    onUploadClick: () -> Unit,
    onCancelUploadClick: () -> Unit,
    onDiscardClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    if (upload != null) {
        // Progress mode: progress bar + cancel button stacked.
        Column(modifier = modifier) {
            val sent = upload.sent
            val total = upload.total.coerceAtLeast(1)
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    text = "uploading frame $sent / $total",
                    style = MaterialTheme.typography.labelMedium,
                    color = pebble.inkSoft,
                )
                Text(
                    text = "${(sent * 100 / total)}%",
                    style = MaterialTheme.typography.labelMedium.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = pebble.ink,
                )
            }
            Spacer(Modifier.height(6.dp))
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(6.dp)
                    .clip(RoundedCornerShape(999.dp))
                    .background(pebble.rule),
            ) {
                Box(
                    modifier = Modifier
                        .fillMaxWidth(sent.toFloat() / total)
                        .height(6.dp)
                        .clip(RoundedCornerShape(999.dp))
                        .background(pebble.accent),
                )
            }
            Spacer(Modifier.height(12.dp))
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(18.dp))
                    .background(pebble.surface)
                    .border(1.dp, pebble.rule, RoundedCornerShape(18.dp))
                    .clickable(onClick = onCancelUploadClick)
                    .padding(vertical = 14.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = "Cancel upload",
                    style = MaterialTheme.typography.titleMedium.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = pebble.ink,
                )
            }
        }
        return
    }

    Row(
        modifier = modifier,
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Upload CTA.
        Box(
            modifier = Modifier
                .weight(1f)
                .clip(RoundedCornerShape(18.dp))
                .background(
                    if (canUpload) pebble.accent
                    else pebble.accent.copy(alpha = 0.4f),
                )
                .clickable(enabled = canUpload && !uploadInProgress, onClick = onUploadClick)
                .padding(vertical = 16.dp),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = "Upload now ↑",
                style = MaterialTheme.typography.titleMedium.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = Color.White,
            )
        }
        // Discard glyph button. Width fixed at 56 dp like the design.
        Box(
            modifier = Modifier
                .size(width = 56.dp, height = 56.dp)
                .clip(RoundedCornerShape(18.dp))
                .background(pebble.surface)
                .border(1.dp, pebble.rule, RoundedCornerShape(18.dp))
                .clickable(onClick = onDiscardClick),
            contentAlignment = Alignment.Center,
        ) {
            Text(
                text = "🗑",
                style = MaterialTheme.typography.bodyLarge,
                color = pebble.danger,
            )
        }
    }
}

@Composable
private fun RenameDialog(
    initial: String,
    onSubmit: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    var value by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(onClick = { onSubmit(value.trim()) }) { Text("Save") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        },
        title = { Text("Rename draft") },
        text = {
            Column {
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
                Spacer(Modifier.height(6.dp))
                Text(
                    text = "leave blank to let the studio pick a memorable " +
                        "name on upload.",
                    style = MaterialTheme.typography.labelSmall,
                    color = pebble.inkSoft,
                )
            }
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
private fun DraftDetailScreenPreview() {
    PebbleTheme {
        DraftDetailScreen(
            state = DraftDetailUiState(
                meta = dev.battleroid.mobilegsscan.DraftMeta(
                    id = "draft1",
                    name = "kitchen — sunset",
                    created_at = "8m ago",
                    updated_at = "8m ago",
                    frame_count = 184,
                    finalized = true,
                ),
                totalBytes = 28L * 1024 * 1024,
                upload = null,
                uploadError = null,
            ),
            onBackClick = {},
            onRenameSubmit = {},
            onUploadClick = {},
            onCancelUploadClick = {},
            onDiscardConfirmed = {},
            onUploadErrorDismiss = {},
        )
    }
}
