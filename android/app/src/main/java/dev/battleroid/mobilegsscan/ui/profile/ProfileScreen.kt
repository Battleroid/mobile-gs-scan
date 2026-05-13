package dev.battleroid.mobilegsscan.ui.profile

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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import dev.battleroid.mobilegsscan.ui.theme.PebbleTheme
import dev.battleroid.mobilegsscan.ui.theme.pebble

/**
 * Profile screen — Pebble's account / device summary surface.
 * Mirrors `studio.jsx:1692` (`StudioAndroidProfile`):
 * back-arrow header + display title; identity card with avatar
 * circle, name, email, paired chip; this-device card with the
 * model + Android version + studio host; menu items linking to
 * Settings + diagnostics; Sign-out and Unpair buttons.
 *
 * Auth fields ([displayName], [email]) are placeholders — Phase 1
 * scoped real auth out. The "Sign out" button currently routes to
 * SignInActivity (a placeholder pair-to-studio screen).
 *
 * Reachable in this PR via the Settings header — there's no slot
 * for a profile button in the design's home header. A dedicated
 * entry point can land when the avatar surface gets real
 * (post-auth).
 */
@Composable
fun ProfileScreen(
    state: ProfileUiState,
    onBackClick: () -> Unit,
    onSettingsClick: () -> Unit,
    onSignOutClick: () -> Unit,
    onUnpairClick: () -> Unit,
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
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            IdentityCard(
                initials = state.initials,
                displayName = state.displayName,
                email = state.email,
                paired = state.paired,
                studioHost = state.studioHost,
            )

            DeviceCard(
                model = state.deviceModel,
                androidVersion = state.androidVersion,
                appVersion = state.appVersion,
                studioHost = state.studioHost.ifBlank { "—" },
            )

            MenuList(onSettingsClick = onSettingsClick)

            SecondaryButton(
                label = "Sign out",
                onClick = onSignOutClick,
                tone = pebble.ink,
            )
            SecondaryButton(
                label = "Unpair this device",
                onClick = onUnpairClick,
                tone = pebble.danger,
            )

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
                .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
                .clickable(onClick = onBackClick),
            contentAlignment = Alignment.Center,
        ) {
            Text("←", style = MaterialTheme.typography.bodyLarge, color = pebble.ink)
        }
        Spacer(Modifier.size(12.dp))
        Text(
            text = "Profile",
            style = MaterialTheme.typography.displaySmall.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.ink,
        )
    }
}

@Composable
private fun IdentityCard(
    initials: String,
    displayName: String,
    email: String,
    paired: Boolean,
    studioHost: String,
) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(18.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(18.dp))
            .padding(18.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        AvatarCircle(initials = initials, size = 72.dp)
        Text(
            text = displayName,
            style = MaterialTheme.typography.displaySmall.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = pebble.ink,
        )
        Text(
            text = email,
            style = MaterialTheme.typography.labelMedium,
            color = pebble.inkSoft,
        )
        Spacer(Modifier.height(2.dp))
        PairedChip(paired = paired, studioHost = studioHost)
    }
}

@Composable
private fun AvatarCircle(initials: String, size: androidx.compose.ui.unit.Dp) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = Modifier
            .size(size)
            .clip(CircleShape)
            .background(pebble.chip2)
            .border(width = 2.dp, color = pebble.surface, shape = CircleShape),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = initials,
            style = MaterialTheme.typography.titleLarge.copy(
                fontWeight = FontWeight.Bold,
            ),
            color = pebble.ink,
        )
    }
}

@Composable
private fun PairedChip(paired: Boolean, studioHost: String) {
    val pebble = MaterialTheme.pebble
    val tone = if (paired) pebble.accent3 else pebble.inkMuted
    val label = when {
        paired && studioHost.isNotBlank() -> "PAIRED · ${studioHost.uppercase()}"
        paired -> "PAIRED"
        else -> "NOT PAIRED"
    }
    Row(
        modifier = Modifier
            .clip(RoundedCornerShape(999.dp))
            .background(tone.copy(alpha = 0.15f))
            .padding(horizontal = 10.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            modifier = Modifier
                .size(6.dp)
                .clip(CircleShape)
                .background(tone),
        )
        Spacer(Modifier.size(6.dp))
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = tone,
        )
    }
}

@Composable
private fun DeviceCard(
    model: String,
    androidVersion: String,
    appVersion: String,
    studioHost: String,
) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp))
            .padding(14.dp),
    ) {
        Text(
            text = "THIS DEVICE",
            style = MaterialTheme.typography.labelSmall,
            color = pebble.accent,
        )
        Spacer(Modifier.height(8.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box(
                modifier = Modifier
                    .size(36.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(pebble.bg)
                    .border(1.dp, pebble.rule, RoundedCornerShape(8.dp)),
                contentAlignment = Alignment.Center,
            ) {
                Text("📱", style = MaterialTheme.typography.bodyLarge)
            }
            Spacer(Modifier.size(10.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = model,
                    style = MaterialTheme.typography.bodyLarge.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = pebble.ink,
                )
                Text(
                    text = "$androidVersion · v$appVersion · $studioHost",
                    style = MaterialTheme.typography.labelSmall,
                    color = pebble.inkSoft,
                )
            }
        }
    }
}

@Composable
private fun MenuList(onSettingsClick: () -> Unit) {
    val pebble = MaterialTheme.pebble
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(1.dp, pebble.rule, RoundedCornerShape(12.dp)),
    ) {
        // Only Settings is wired today; the rest of the design's
        // menu items (Notifications, Privacy, Help) don't have
        // backing surfaces yet. Keep them off the screen so we don't
        // ship dead links.
        MenuItem(
            icon = "⚙",
            label = "Settings",
            sub = "capture, AR, Studio URL",
            onClick = onSettingsClick,
            last = true,
        )
    }
}

@Composable
private fun MenuItem(
    icon: String,
    label: String,
    sub: String,
    onClick: () -> Unit,
    last: Boolean = false,
) {
    val pebble = MaterialTheme.pebble
    Column {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable(onClick = onClick)
                .padding(horizontal = 14.dp, vertical = 13.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(30.dp)
                    .clip(RoundedCornerShape(8.dp))
                    .background(pebble.bg)
                    .border(1.dp, pebble.rule, RoundedCornerShape(8.dp)),
                contentAlignment = Alignment.Center,
            ) {
                Text(icon, style = MaterialTheme.typography.bodyMedium)
            }
            Spacer(Modifier.size(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = label,
                    style = MaterialTheme.typography.bodyLarge.copy(
                        fontWeight = FontWeight.SemiBold,
                    ),
                    color = pebble.ink,
                )
                Text(
                    text = sub,
                    style = MaterialTheme.typography.labelSmall,
                    color = pebble.inkSoft,
                )
            }
            Text(
                text = "›",
                style = MaterialTheme.typography.bodyLarge,
                color = pebble.inkMuted,
            )
        }
        if (!last) {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(1.dp)
                    .background(pebble.rule),
            )
        }
    }
}

@Composable
private fun SecondaryButton(
    label: String,
    onClick: () -> Unit,
    tone: Color,
) {
    val pebble = MaterialTheme.pebble
    Box(
        modifier = Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(pebble.surface)
            .border(
                width = 1.dp,
                color = if (tone == pebble.danger) {
                    pebble.danger.copy(alpha = 0.35f)
                } else {
                    pebble.rule
                },
                shape = RoundedCornerShape(12.dp),
            )
            .clickable(onClick = onClick)
            .padding(vertical = 14.dp),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.titleMedium.copy(
                fontWeight = FontWeight.SemiBold,
            ),
            color = tone,
        )
    }
}

@androidx.compose.ui.tooling.preview.Preview(
    showBackground = true,
    widthDp = 360,
    heightDp = 800,
)
@Composable
private fun ProfileScreenPreview() {
    PebbleTheme {
        ProfileScreen(
            state = ProfileUiState(
                initials = "MW",
                displayName = "Mira Weston",
                email = "mira@studio.local",
                paired = true,
                studioHost = "192.168.1.42",
                deviceModel = "Pixel 8 Pro",
                androidVersion = "Android 15",
                appVersion = "1.0.0",
            ),
            onBackClick = {},
            onSettingsClick = {},
            onSignOutClick = {},
            onUnpairClick = {},
        )
    }
}
