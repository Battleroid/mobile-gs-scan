package dev.battleroid.mobilegsscan.ui.detail

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
import androidx.compose.foundation.layout.heightIn
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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement

/**
 * Job detail screen — Compose port of the legacy XML-driven
 * `JobDetailActivity`. Renders the polled job row (kind, status,
 * progress, message, timestamps, error, optional result blob)
 * plus a collapsible subprocess-log panel.
 *
 * Behaviour preserved verbatim:
 *  - Auto-open the log panel the first time the job is observed
 *    `running` or `claimed`; the activity owns that flip via
 *    [LogPanelState.open] and we just render its state.
 *  - Manual toggle via the "show log" / "hide log" pill.
 *  - Result is pretty-printed JSON (worker-specific shape — sfm
 *    output, train metrics, export paths — too varied to type).
 *  - Log panel auto-scrolls to its newest line on every content
 *    change.
 *
 * Mirrors `studio.jsx:897` (`StudioAndroidStep`) for the visual
 * shape: cream paper, back-arrow header, display title (job kind)
 * with status mono kicker, progress bar surface card, metrics
 * grid, mono log panel.
 */
private val prettyJson = Json { prettyPrint = true; encodeDefaults = false }

@Composable
fun JobDetailScreen(
    state: JobDetailUiState,
    onBackClick: () -> Unit,
    onToggleLog: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val pebble = MaterialTheme.pebble
    val scroll = rememberScrollState()
    val job = state.job

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
        JobHeader(
            status = job?.status,
            networkError = state.networkError,
            onBackClick = onBackClick,
        )

        Column(
            modifier = Modifier
                .weight(1f)
                .fillMaxWidth()
                .verticalScroll(scroll)
                .padding(horizontal = 20.dp)
                .padding(top = 8.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Text(
                text = state.kind.ifBlank { "loading…" },
                style = MaterialTheme.typography.displayMedium.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
            )

            if (job != null) {
                ProgressCard(
                    progress = job.progress,
                    message = job.progress_msg,
                    status = job.status,
                )
                MetricsCard(
                    claimedBy = job.claimed_by,
                    startedAt = job.started_at,
                    completedAt = job.completed_at,
                )
                if (!job.error.isNullOrBlank()) {
                    ErrorBlock(job.error)
                }
                if (job.result != null) {
                    ResultBlock(job.result)
                }
            }

            LogPanel(
                state = state.log,
                onToggle = onToggleLog,
            )

            Spacer(
                Modifier.windowInsetsPadding(
                    WindowInsets.systemBars.only(WindowInsetsSides.Bottom)
                ).height(24.dp),
            )
        }
    }
}

@Composable
private fun JobHeader(
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
                .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
                .clickable(onClick = onBackClick),
            contentAlignment = Alignment.Center,
        ) {
            Text("←", style = MaterialTheme.typography.bodyLarge, color = pebble.ink)
        }
        Spacer(Modifier.size(14.dp))
        val tone = when {
            networkError != null -> pebble.danger
            status == "completed" -> pebble.accent3
            status == "running" || status == "claimed" -> pebble.accent
            status == "failed" || status == "canceled" -> pebble.danger
            else -> pebble.inkMuted
        }
        val label = networkError?.let { "offline · $it" } ?: status ?: "loading…"
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
}

@Composable
private fun ProgressCard(progress: Float, message: String?, status: String) {
    val pebble = MaterialTheme.pebble
    val tone = when (status) {
        "completed" -> pebble.accent3
        "running", "claimed" -> pebble.accent
        "failed", "canceled" -> pebble.danger
        else -> pebble.inkMuted
    }
    val visualPct = when (status) {
        "completed", "canceled" -> 1f
        else -> progress.coerceIn(0f, 1f)
    }
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .padding(horizontal = 16.dp, vertical = 14.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = status.uppercase(),
                style = MaterialTheme.typography.labelSmall,
                color = tone,
            )
            Text(
                text = "${(visualPct * 100).toInt()}%",
                style = MaterialTheme.typography.titleMedium.copy(
                    fontWeight = FontWeight.SemiBold,
                ),
                color = pebble.ink,
            )
        }
        Spacer(Modifier.height(8.dp))
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(6.dp)
                .clip(RoundedCornerShape(999.dp))
                .background(pebble.rule),
        ) {
            Box(
                modifier = Modifier
                    .fillMaxWidth(visualPct)
                    .height(6.dp)
                    .clip(RoundedCornerShape(999.dp))
                    .background(tone),
            )
        }
        if (!message.isNullOrBlank()) {
            Spacer(Modifier.height(8.dp))
            Text(
                text = message,
                style = MaterialTheme.typography.labelSmall,
                color = pebble.inkSoft,
            )
        }
    }
}

@Composable
private fun MetricsCard(
    claimedBy: String?,
    startedAt: String?,
    completedAt: String?,
) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        MetricsRow(label = "claimed by", value = claimedBy ?: "(unclaimed)")
        MetricsRow(label = "started", value = startedAt ?: "—")
        MetricsRow(label = "completed", value = completedAt ?: "—")
    }
}

@Composable
private fun MetricsRow(label: String, value: String) {
    val pebble = MaterialTheme.pebble
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(
            text = label.uppercase(),
            style = MaterialTheme.typography.labelSmall,
            color = pebble.inkMuted,
        )
        Text(
            text = value,
            style = MaterialTheme.typography.labelMedium,
            color = pebble.inkSoft,
        )
    }
}

@Composable
private fun ErrorBlock(error: String) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.danger.copy(alpha = 0.08f))
            .border(1.dp, pebble.danger.copy(alpha = 0.4f), RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        Text("ERROR", style = MaterialTheme.typography.labelSmall, color = pebble.danger)
        Spacer(Modifier.height(4.dp))
        Text(error, style = MaterialTheme.typography.bodyMedium, color = pebble.ink)
    }
}

@Composable
private fun ResultBlock(result: JsonElement) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        Text("RESULT", style = MaterialTheme.typography.labelSmall, color = pebble.inkMuted)
        Spacer(Modifier.height(6.dp))
        Text(
            // Pretty JSON dump — worker output is freeform, the user
            // here is usually debugging a failed job and just wants
            // the raw payload.
            text = prettyJson.encodeToString(JsonElement.serializer(), result),
            style = MaterialTheme.typography.labelSmall.copy(
                fontFamily = MaterialTheme.typography.labelMedium.fontFamily,
            ),
            color = pebble.inkSoft,
        )
    }
}

@Composable
private fun LogPanel(
    state: LogPanelState,
    onToggle: () -> Unit,
) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .padding(horizontal = 14.dp, vertical = 12.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = "SUBPROCESS LOG",
                style = MaterialTheme.typography.labelSmall,
                color = pebble.inkMuted,
            )
            Box(
                modifier = Modifier
                    .clip(RoundedCornerShape(999.dp))
                    .background(pebble.bgAlt)
                    .clickable(onClick = onToggle)
                    .padding(horizontal = 10.dp, vertical = 4.dp),
            ) {
                Text(
                    text = if (state.open) "hide log" else "show log",
                    style = MaterialTheme.typography.labelSmall,
                    color = pebble.ink,
                )
            }
        }
        if (state.open) {
            Spacer(Modifier.height(8.dp))
            LogBody(state = state)
        }
    }
}

@Composable
private fun LogBody(state: LogPanelState) {
    val pebble = MaterialTheme.pebble
    val scrollState = rememberScrollState()
    val scope = rememberCoroutineScope()

    // Auto-pin to the latest line on every content change — same
    // semantics the legacy implementation enforced via TextView's
    // internal scroll. The post-layout dispatch is mirrored by
    // LaunchedEffect on the content string: after recomposition
    // settles, scroll to the bottom of the content area.
    LaunchedEffect(state.content) {
        if (state.open) {
            scope.launch { scrollState.scrollTo(scrollState.maxValue) }
        }
    }

    val body = when {
        state.fetchError != null -> "could not load log: ${state.fetchError}"
        !state.available -> "no log available for this step"
        state.content.isNullOrBlank() -> "(log file is empty)"
        else -> state.content
    }
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .heightIn(max = 280.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(Color.Black.copy(alpha = 0.04f))
            .padding(horizontal = 10.dp, vertical = 8.dp)
            .verticalScroll(scrollState),
    ) {
        Text(
            text = body,
            style = MaterialTheme.typography.labelSmall.copy(
                fontFamily = MaterialTheme.typography.labelMedium.fontFamily,
            ),
            color = pebble.inkSoft,
        )
    }
}
