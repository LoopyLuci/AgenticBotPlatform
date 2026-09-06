package com.botserver.mobile.data

import android.content.Context
import com.botserver.mobile.ui.appearance.AccentColor
import com.botserver.mobile.ui.appearance.AppIcon
import com.botserver.mobile.ui.appearance.ThemeMode
import com.botserver.mobile.ui.appearance.UiDensity
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Local, per-device presentation preferences — app icon, theme mode,
 * accent color, UI density. Deliberately **not** routed through
 * SettingsRepository's `POST /api/config/set` the way everything else
 * in Settings is: those are server-side bot config, shared and synced
 * across every device that pairs to this server (see
 * SettingsViewModel's doc comment). How *this* phone wants its own
 * launcher icon and color scheme to look is a device-local choice, the
 * same way the desktop app's theme/scale picks are `localStorage`-only
 * (see desktop-app/ui/main.js's applyAppearanceTheme/applyAppearanceScale) —
 * so plain (unencrypted — nothing sensitive here, unlike CredentialStore)
 * SharedPreferences is the right store, not a server round-trip.
 */
@Singleton
class AppearanceStore @Inject constructor(@ApplicationContext context: Context) {

    private val prefs = context.getSharedPreferences("botserver_appearance", Context.MODE_PRIVATE)

    private val _icon = MutableStateFlow(AppIcon.fromStorageKey(prefs.getString(KEY_ICON, null)))
    val icon: StateFlow<AppIcon> = _icon.asStateFlow()

    private val _themeMode = MutableStateFlow(ThemeMode.fromStorageKey(prefs.getString(KEY_THEME, null)))
    val themeMode: StateFlow<ThemeMode> = _themeMode.asStateFlow()

    private val _accent = MutableStateFlow(AccentColor.fromStorageKey(prefs.getString(KEY_ACCENT, null)))
    val accent: StateFlow<AccentColor> = _accent.asStateFlow()

    private val _density = MutableStateFlow(UiDensity.fromStorageKey(prefs.getString(KEY_DENSITY, null)))
    val density: StateFlow<UiDensity> = _density.asStateFlow()

    fun setIcon(value: AppIcon) {
        prefs.edit().putString(KEY_ICON, value.storageKey).apply()
        _icon.value = value
    }

    fun setThemeMode(value: ThemeMode) {
        prefs.edit().putString(KEY_THEME, value.storageKey).apply()
        _themeMode.value = value
    }

    fun setAccent(value: AccentColor) {
        prefs.edit().putString(KEY_ACCENT, value.storageKey).apply()
        _accent.value = value
    }

    fun setDensity(value: UiDensity) {
        prefs.edit().putString(KEY_DENSITY, value.storageKey).apply()
        _density.value = value
    }

    companion object {
        private const val KEY_ICON = "icon"
        private const val KEY_THEME = "theme_mode"
        private const val KEY_ACCENT = "accent"
        private const val KEY_DENSITY = "density"
    }
}
