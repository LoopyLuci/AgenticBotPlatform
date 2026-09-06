package com.botserver.mobile.ui.appearance

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.AppearanceStore
import dagger.hilt.android.lifecycle.HiltViewModel
import javax.inject.Inject
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn

data class AppearanceUiState(
    val icon: AppIcon = AppIcon.DEFAULT,
    val themeMode: ThemeMode = ThemeMode.DEFAULT,
    val accent: AccentColor = AccentColor.DEFAULT,
    val density: UiDensity = UiDensity.DEFAULT,
)

/** Backs the Settings screen's "Appearance" card and (via [uiState]'s
 * themeMode/accent/density) the running app's actual MaterialTheme —
 * both read the same [AppearanceStore], so a change here is visible
 * immediately with no separate "apply" step. AndroidViewModel (not
 * plain ViewModel) because [AppIconSwitcher.applyIcon] needs a
 * Context and this is simpler than adding a Hilt `@ApplicationContext`
 * qualifier just for that one call. */
@HiltViewModel
class AppearanceViewModel @Inject constructor(
    application: Application,
    private val store: AppearanceStore,
) : AndroidViewModel(application) {

    val uiState: StateFlow<AppearanceUiState> = combine(
        store.icon, store.themeMode, store.accent, store.density,
    ) { icon, themeMode, accent, density ->
        AppearanceUiState(icon, themeMode, accent, density)
    }.stateIn(viewModelScope, SharingStarted.Eagerly, AppearanceUiState())

    fun setIcon(icon: AppIcon) {
        AppIconSwitcher.applyIcon(getApplication(), icon)
        store.setIcon(icon)
    }

    fun setThemeMode(mode: ThemeMode) = store.setThemeMode(mode)
    fun setAccent(accent: AccentColor) = store.setAccent(accent)
    fun setDensity(density: UiDensity) = store.setDensity(density)
}
