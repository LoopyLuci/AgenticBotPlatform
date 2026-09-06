package com.botserver.mobile.ui.appearance

import androidx.compose.ui.graphics.Color
import com.botserver.mobile.R
import com.botserver.mobile.ui.theme.TgAccent
import com.botserver.mobile.ui.theme.TgAccent2
import com.botserver.mobile.ui.theme.TgAccentDark
import com.botserver.mobile.ui.theme.TgAccentDark2

/** One of the four selectable launcher icons, each backed by its own
 * `<activity-alias>` in AndroidManifest.xml (see AppIconSwitcher) —
 * exactly one is enabled on the device at a time. Vaporwave is the
 * shipped default (also what `@mipmap/ic_launcher` itself resolves to,
 * so a component with no explicit icon — e.g. the FCM notification —
 * still matches whatever a fresh install shows). */
enum class AppIcon(val storageKey: String, val label: String, val aliasName: String, val previewRes: Int) {
    VAPORWAVE("vaporwave", "Vaporwave Visor", "com.botserver.mobile.IconAliasVaporwave", R.drawable.icon_preview_vaporwave),
    CUTE("cute", "Cute Pastel", "com.botserver.mobile.IconAliasCute", R.drawable.icon_preview_cute),
    HOLO("holo", "Holo Orb", "com.botserver.mobile.IconAliasHolo", R.drawable.icon_preview_holo),
    CYBERPUNK("cyberpunk", "Cyberpunk", "com.botserver.mobile.IconAliasCyberpunk", R.drawable.icon_preview_cyberpunk),
    ;

    companion object {
        val DEFAULT = VAPORWAVE
        fun fromStorageKey(key: String?): AppIcon = entries.firstOrNull { it.storageKey == key } ?: DEFAULT
    }
}

enum class ThemeMode(val storageKey: String, val label: String) {
    SYSTEM("system", "System"),
    LIGHT("light", "Light"),
    DARK("dark", "Dark"),
    ;

    companion object {
        val DEFAULT = SYSTEM
        fun fromStorageKey(key: String?): ThemeMode = entries.firstOrNull { it.storageKey == key } ?: DEFAULT
    }
}

enum class UiDensity(val storageKey: String, val label: String) {
    COMFORTABLE("comfortable", "Comfortable"),
    COMPACT("compact", "Compact"),
    ;

    companion object {
        val DEFAULT = COMFORTABLE
        fun fromStorageKey(key: String?): UiDensity = entries.firstOrNull { it.storageKey == key } ?: DEFAULT
    }
}

/** A named accent seed. BLUE reuses the app's original Telegram-blue
 * palette verbatim (TgAccent/TgAccentDark from ui/theme/Color.kt) so
 * picking it is a true no-op; the other four are lighter-weight — just
 * a primary/secondary pair per light/dark mode — with container/ink
 * tones derived at theme-build time (see Theme.kt's `withAccent`)
 * rather than hand-specifying a 5-color set per accent like the base
 * palette does, since these are meant to be quick, low-stakes picks. */
enum class AccentColor(
    val storageKey: String,
    val label: String,
    val primaryLight: Color,
    val secondaryLight: Color,
    val primaryDark: Color,
    val secondaryDark: Color,
) {
    BLUE("blue", "Blue", TgAccent, TgAccent2, TgAccentDark, TgAccentDark2),
    PINK("pink", "Pink", Color(0xFFE0568F), Color(0xFFC2447A), Color(0xFFFF8FC6), Color(0xFFFF6FB2)),
    VIOLET("violet", "Violet", Color(0xFF7A4FE0), Color(0xFF5E39B8), Color(0xFFB07AFF), Color(0xFF9662E8)),
    CYAN("cyan", "Cyan", Color(0xFF1AA9C2), Color(0xFF12839A), Color(0xFF4EE0FF), Color(0xFF2EC8EE)),
    MAGENTA("magenta", "Magenta", Color(0xFFCC2C9E), Color(0xFFA3227E), Color(0xFFFF4FC3), Color(0xFFE83EAE)),
    ;

    companion object {
        val DEFAULT = BLUE
        fun fromStorageKey(key: String?): AccentColor = entries.firstOrNull { it.storageKey == key } ?: DEFAULT
    }
}
