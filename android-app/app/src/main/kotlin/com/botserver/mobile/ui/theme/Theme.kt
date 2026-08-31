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
    MaterialTheme(colorScheme = colorScheme, shapes = TgShapes, typography = BotServerTypography, content = content)
}
