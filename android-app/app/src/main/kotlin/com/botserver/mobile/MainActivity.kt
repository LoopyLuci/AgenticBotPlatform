package com.botserver.mobile

import android.Manifest
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.*
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.botserver.mobile.data.CredentialStore
import com.botserver.mobile.ui.nav.HomeScreen
import com.botserver.mobile.ui.pairing.PairingScreen
import com.botserver.mobile.ui.theme.BotServerTheme
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject

@AndroidEntryPoint
class MainActivity : ComponentActivity() {

    @Inject lateinit var credentials: CredentialStore

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // A botserver://pair link — whether tapped from a shared link or
        // fired by the desktop app's adb auto-pair flow (`adb shell am
        // start -a android.intent.action.VIEW -d 'botserver://pair?...'`)
        // — lands here as this activity's launch intent. Its raw data
        // string is handed to PairingScreen so it can skip straight to
        // verifying instead of waiting for a QR scan or manual entry.
        val autoPairRaw = intent?.data?.toString()

        setContent {
            val notificationPermissionLauncher = rememberLauncherForActivityResult(
                ActivityResultContracts.RequestPermission(),
            ) {}
            LaunchedEffect(Unit) {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                }
            }
            BotServerTheme {
                val navController = rememberNavController()
                // A fresh botserver://pair link always wins, even if this
                // device already has stored credentials — otherwise
                // re-pairing (the desktop's auto-pair flow, or a shared
                // link) silently no-ops on an already-paired device: the
                // dashboard mints a new key and shows it as paired, but the
                // phone never adopts it and keeps 401ing on its old
                // (possibly since-revoked) one. Routing to "pairing"
                // whenever a link was actually delivered lets
                // PairingScreen's onAutoPairLink() (see PairingViewModel)
                // verify and overwrite the stored credentials with the new
                // key before landing on "home".
                val startDestination = if (credentials.isPaired && autoPairRaw == null) "home" else "pairing"

                NavHost(navController = navController, startDestination = startDestination) {
                    composable("pairing") {
                        PairingScreen(
                            autoPairRaw = autoPairRaw,
                            onPaired = {
                                navController.navigate("home") {
                                    popUpTo("pairing") { inclusive = true }
                                }
                            },
                        )
                    }
                    composable("home") {
                        HomeScreen()
                    }
                }
            }
        }
    }
}
