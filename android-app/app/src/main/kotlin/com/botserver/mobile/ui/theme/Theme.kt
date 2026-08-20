package com.botserver.mobile.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp

// Same palette as the desktop dashboard (desktop-app/ui/index.html) so the
// phone app and the desktop control panel read as one product, not two.
// Deliberately NOT using dynamicDarkColorScheme/dynamicLightColorScheme —
// Material You would recolor everything to the device wallpaper, which is
// the opposite of "look like Telegram."
private val TgAccent = Color(0xFF3390EC)
private val TgAccent2 = Color(0xFF2478D4)
private val TgAccentSoft = Color(0xFFE3F1FF)
private val TgAccentInk = Color(0xFF1467BF)
private val TgLightBg = Color(0xFFE6EBEE)
private val TgLightSurface = Color(0xFFFFFFFF)
private val TgLightSurfaceVariant = Color(0xFFF4F6F8)
private val TgLightInk = Color(0xFF1C2733)
private val TgLightOutline = Color(0xFFE2E7EB)
private val TgError = Color(0xFFE0473E)

private val TgAccentDark = Color(0xFF4EA4F5)
private val TgAccentDark2 = Color(0xFF2F8CE0)
private val TgAccentSoftDark = Color(0xFF20344A)
private val TgAccentInkDark = Color(0xFFBFE0FF)
private val TgDarkBg = Color(0xFF0E1621)
private val TgDarkSurface = Color(0xFF17212B)
private val TgDarkSurfaceVariant = Color(0xFF1E2C3A)
private val TgDarkInk = Color(0xFFE9EDF1)
private val TgDarkOutline = Color(0xFF0C141D)

private val TgLightColors = lightColorScheme(
    primary = TgAccent,
    onPrimary = Color.White,
    primaryContainer = TgAccentSoft,
    onPrimaryContainer = TgAccentInk,
    secondary = TgAccent2,
    onSecondary = Color.White,
    background = TgLightBg,
    onBackground = TgLightInk,
    surface = TgLightSurface,
    onSurface = TgLightInk,
    surfaceVariant = TgLightSurfaceVariant,
    onSurfaceVariant = TgLightInk,
    outline = TgLightOutline,
    error = TgError,
    onError = Color.White,
)

private val TgDarkColors = darkColorScheme(
    primary = TgAccentDark,
    onPrimary = TgDarkBg,
    primaryContainer = TgAccentSoftDark,
    onPrimaryContainer = TgAccentInkDark,
    secondary = TgAccentDark2,
    onSecondary = TgDarkBg,
    background = TgDarkBg,
    onBackground = TgDarkInk,
    surface = TgDarkSurface,
    onSurface = TgDarkInk,
    surfaceVariant = TgDarkSurfaceVariant,
    onSurfaceVariant = TgDarkInk,
    outline = TgDarkOutline,
    error = TgError,
    onError = Color.White,
)

// Telegram rounds everything generously — bubbles, sheets, buttons, fields.
private val TgShapes = Shapes(
    extraSmall = RoundedCornerShape(8.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(16.dp),
    large = RoundedCornerShape(20.dp),
    extraLarge = RoundedCornerShape(28.dp),
)

@Composable
fun BotServerTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    val colorScheme = if (darkTheme) TgDarkColors else TgLightColors
    MaterialTheme(colorScheme = colorScheme, shapes = TgShapes, content = content)
}
