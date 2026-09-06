package com.botserver.mobile.data

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.botserver.mobile.ui.appearance.AccentColor
import com.botserver.mobile.ui.appearance.AppIcon
import com.botserver.mobile.ui.appearance.ThemeMode
import com.botserver.mobile.ui.appearance.UiDensity
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/** AppearanceStore needs a real Context for SharedPreferences (this
 * codebase avoids Robolectric — see PairingRepositoryTest's own history
 * of dropping android.net.Uri for exactly this reason), so this is an
 * instrumented test, not a plain JVM unit test. Defaults-before-any-
 * write and round-trip-after-write are the two behaviors that matter:
 * everything else (AppIconSwitcher's component toggling, the actual
 * MaterialTheme recolor) is exercised by the Compose UI instead. */
@RunWith(AndroidJUnit4::class)
class AppearanceStoreTest {

    private lateinit var store: AppearanceStore

    @Before
    fun setUp() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        context.getSharedPreferences("botserver_appearance", android.content.Context.MODE_PRIVATE).edit().clear().commit()
        store = AppearanceStore(context)
    }

    @Test
    fun defaultsAreVaporwaveSystemBlueComfortable() {
        assertEquals(AppIcon.VAPORWAVE, store.icon.value)
        assertEquals(ThemeMode.SYSTEM, store.themeMode.value)
        assertEquals(AccentColor.BLUE, store.accent.value)
        assertEquals(UiDensity.COMFORTABLE, store.density.value)
    }

    @Test
    fun eachSetterPersistsAndUpdatesTheFlowImmediately() {
        store.setIcon(AppIcon.CYBERPUNK)
        store.setThemeMode(ThemeMode.DARK)
        store.setAccent(AccentColor.MAGENTA)
        store.setDensity(UiDensity.COMPACT)

        assertEquals(AppIcon.CYBERPUNK, store.icon.value)
        assertEquals(ThemeMode.DARK, store.themeMode.value)
        assertEquals(AccentColor.MAGENTA, store.accent.value)
        assertEquals(UiDensity.COMPACT, store.density.value)

        // A fresh instance reading the same underlying prefs sees the
        // same values — proves it's actually persisted, not just an
        // in-memory field on this one instance.
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val reloaded = AppearanceStore(context)
        assertEquals(AppIcon.CYBERPUNK, reloaded.icon.value)
        assertEquals(ThemeMode.DARK, reloaded.themeMode.value)
        assertEquals(AccentColor.MAGENTA, reloaded.accent.value)
        assertEquals(UiDensity.COMPACT, reloaded.density.value)
    }
}
