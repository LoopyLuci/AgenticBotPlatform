package com.botserver.mobile.ui.update

import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.botserver.mobile.BuildConfig

/** The full self-update experience — installed version, a manual "Check
 * for updates" button, and [AppUpdateBanner]'s available/downloading/
 * error states — factored out of Settings' old AppUpdateCard so
 * PairingScreen can show the exact same thing, not just the passive
 * auto-check banner. Before this, the pairing screen only ever showed
 * *something* if a check both ran and found a real update; if the
 * device was already current (or the check silently failed and the user
 * didn't notice the small error banner), there was no visible "check for
 * updates yourself" affordance at all pre-pairing — this is that
 * affordance, always present regardless of what the last check found. */
@Composable
fun AppUpdateStatus(updateViewModel: AppUpdateViewModel) {
    val state by updateViewModel.state.collectAsState()

    Text(
        "Installed version: ${BuildConfig.RELEASE_TAG}",
        style = MaterialTheme.typography.bodySmall,
    )
    Spacer(Modifier.height(8.dp))
    when (state) {
        is AppUpdateState.Checking -> Row(verticalAlignment = Alignment.CenterVertically) {
            CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
            Spacer(Modifier.width(8.dp))
            Text("Checking GitHub…", style = MaterialTheme.typography.labelSmall)
        }
        is AppUpdateState.UpToDate -> Text("You're on the latest published release.", style = MaterialTheme.typography.labelSmall)
        else -> AppUpdateBanner(updateViewModel)
    }
    if (state is AppUpdateState.Idle || state is AppUpdateState.UpToDate || state is AppUpdateState.Error) {
        Spacer(Modifier.height(8.dp))
        OutlinedButton(onClick = updateViewModel::checkForUpdate, modifier = Modifier.testTag("update-check")) {
            Text("Check for updates")
        }
    }
}
