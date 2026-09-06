package com.botserver.mobile.ui.appearance

import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager

/**
 * Flips which of the four `<activity-alias>` launcher entries
 * (AndroidManifest.xml) is enabled, so the home-screen/app-drawer icon
 * changes without a reinstall — the standard Android "dynamic app icon"
 * technique (the same mechanism apps like Slack/Instagram use for
 * seasonal icons). `MainActivity` itself carries no MAIN/LAUNCHER
 * intent-filter; exactly one alias (all targeting MainActivity) is
 * enabled at a time. `DONT_KILL_APP` avoids force-killing this process
 * mid-change — some launchers still refresh the visible icon almost
 * immediately, others only after the next home-screen visit; both are
 * normal, expected behavior for this technique, not a bug here.
 */
object AppIconSwitcher {

    fun applyIcon(context: Context, icon: AppIcon) {
        val pm = context.packageManager
        AppIcon.entries.forEach { candidate ->
            val alias = ComponentName(context.packageName, candidate.aliasName)
            val state = if (candidate == icon) {
                PackageManager.COMPONENT_ENABLED_STATE_ENABLED
            } else {
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED
            }
            pm.setComponentEnabledSetting(alias, state, PackageManager.DONT_KILL_APP)
        }
    }
}
