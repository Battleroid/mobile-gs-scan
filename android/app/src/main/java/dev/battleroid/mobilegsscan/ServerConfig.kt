package dev.battleroid.mobilegsscan

import android.content.Context
import android.content.SharedPreferences
import androidx.core.content.edit

/**
 * Persists the studio URL ("https://studio.local" or
 * "https://192.168.1.42") between launches. Single-user, single-server
 * — there's no list of saved servers, just the most recent one.
 */
object ServerConfig {
    private const val PREFS = "studio"
    private const val KEY_URL = "studio_url"

    fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun studioUrl(ctx: Context): String? =
        prefs(ctx).getString(KEY_URL, null)?.takeIf { it.isNotBlank() }

    fun setStudioUrl(ctx: Context, url: String) {
        prefs(ctx).edit { putString(KEY_URL, normalize(url)) }
    }

    /**
     * Normalize a user-typed studio URL.
     *
     *   - trim whitespace
     *   - drop a trailing `/`
     *   - prepend `https://` if no scheme is present (`make up-https`
     *     is the documented dev path; users mostly type bare IPs and
     *     hostnames). Users who genuinely want plain `http` can type
     *     it explicitly.
     *
     * Idempotent: a URL that already starts with `http://` or
     * `https://` is left alone.
     */
    fun normalize(raw: String): String {
        val trimmed = raw.trim().trimEnd('/')
        if (trimmed.isEmpty()) return ""
        val lower = trimmed.lowercase()
        if (lower.startsWith("http://") || lower.startsWith("https://")) {
            return trimmed
        }
        return "https://$trimmed"
    }
}
