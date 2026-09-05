package com.botserver.mobile.ui.update

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

/** Compact "an update is available" banner, only rendered when there's
 * actually something to say — silent for Idle/Checking/UpToDate so it
 * never crowds a screen (the pairing screen especially) with a
 * check-in-progress spinner for something the user didn't ask about.
 * Shared between PairingScreen (reachable with no pairing at all) and
 * Settings, so "check for updates" behaves identically everywhere it
 * appears — see AppUpdateViewModel's own doc for why this is one shared
 * view-model class rather than two copies of the same state machine. */
@Composable
fun AppUpdateBanner(viewModel: AppUpdateViewModel) {
    val context = LocalContext.current
    val state by viewModel.state.collectAsState()

    when (val s = state) {
        is AppUpdateState.Available -> Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 3.dp, modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(14.dp)) {
                Text("App update available: ${s.release.tag}", style = MaterialTheme.typography.bodyMedium)
                Spacer(Modifier.height(8.dp))
                Row {
                    Button(onClick = viewModel::downloadAndInstall, modifier = Modifier.testTag("update-banner-install")) {
                        Text("Update now")
                    }
                    Spacer(Modifier.width(8.dp))
                    TextButton(onClick = viewModel::dismiss) { Text("Not now") }
                }
            }
        }
        is AppUpdateState.Downloading -> Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 3.dp, modifier = Modifier.fillMaxWidth()) {
            Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
                Spacer(Modifier.width(10.dp))
                Text("Downloading update…", style = MaterialTheme.typography.bodyMedium)
            }
        }
        is AppUpdateState.Downloaded -> {
            // The one tap this can't skip: Android's own install
            // confirmation dialog — no ordinary app can install a
            // sideloaded APK without it. Firing the intent immediately on
            // download completing means that OS prompt is the very next
            // thing the user sees, not one more button to find first.
            LaunchedEffect(s.file) { context.startActivity(viewModel.installIntent(s.file)) }
            Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 3.dp, modifier = Modifier.fillMaxWidth()) {
                Text(
                    "Downloaded — confirm the install when Android asks.",
                    modifier = Modifier.padding(14.dp),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }
        is AppUpdateState.Error -> Surface(shape = RoundedCornerShape(12.dp), tonalElevation = 3.dp, modifier = Modifier.fillMaxWidth()) {
            Row(Modifier.padding(14.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(s.message, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("update-banner-error"))
                Spacer(Modifier.width(8.dp))
                TextButton(onClick = viewModel::dismiss) { Text("Dismiss") }
            }
        }
        AppUpdateState.Idle, AppUpdateState.Checking, AppUpdateState.UpToDate -> {}
    }
}
