package com.botserver.mobile.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.unit.dp
import com.botserver.mobile.ui.appearance.AccentColor
import com.botserver.mobile.ui.appearance.UiDensity

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

/** A slightly tighter type scale for [UiDensity.COMPACT] — every size
 * scaled down from [BotServerTypography] rather than hand-tuned, so the
 * two scales can never drift out of proportion with each other. */
private val CompactTypography: Typography = BotServerTypography.let { base ->
    fun scale(style: androidx.compose.ui.text.TextStyle) = style.copy(
        fontSize = style.fontSize * 0.92f,
        lineHeight = style.lineHeight * 0.92f,
    )
    Typography(
        titleLarge = scale(base.titleLarge),
        titleMedium = scale(base.titleMedium),
        titleSmall = scale(base.titleSmall),
        bodyLarge = scale(base.bodyLarge),
        bodyMedium = scale(base.bodyMedium),
        bodySmall = scale(base.bodySmall),
        labelLarge = scale(base.labelLarge),
        labelMedium = scale(base.labelMedium),
        labelSmall = scale(base.labelSmall),
    )
}

/** Re-tints a base (light or dark) color scheme's primary/secondary
 * axis to a chosen [AccentColor], deriving container/ink tones by
 * blending toward white (light mode) or black (dark mode) rather than
 * requiring a hand-specified 5-color set per accent like the app's
 * original Telegram-blue palette has — these are meant to be quick,
 * low-stakes picks, not a from-scratch palette each. BLUE is a no-op
 * (returns [base] unchanged) since it *is* that original palette. */
private fun ColorScheme.withAccent(accent: AccentColor, darkTheme: Boolean): ColorScheme {
    if (accent == AccentColor.BLUE) return this
    val primary = if (darkTheme) accent.primaryDark else accent.primaryLight
    val secondary = if (darkTheme) accent.secondaryDark else accent.secondaryLight
    val container = lerp(primary, if (darkTheme) Color.Black else Color.White, if (darkTheme) 0.72f else 0.85f)
    val onContainer = lerp(primary, if (darkTheme) Color.White else Color.Black, if (darkTheme) 0.35f else 0.30f)
    val onPrimary = if (darkTheme) TgDarkBg else Color.White
    return copy(
        primary = primary,
        onPrimary = onPrimary,
        primaryContainer = container,
        onPrimaryContainer = onContainer,
        secondary = secondary,
        onSecondary = onPrimary,
    )
}

@Composable
fun BotServerTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    accent: AccentColor = AccentColor.DEFAULT,
    density: UiDensity = UiDensity.DEFAULT,
    content: @Composable () -> Unit,
) {
    val base = if (darkTheme) TgDarkColors else TgLightColors
    val colorScheme = base.withAccent(accent, darkTheme)
    val typography = if (density == UiDensity.COMPACT) CompactTypography else BotServerTypography
    MaterialTheme(colorScheme = colorScheme, shapes = TgShapes, typography = typography, content = content)
}
