package com.botserver.mobile.ui.pairing

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.hilt.navigation.compose.hiltViewModel
import com.botserver.mobile.ui.update.AppUpdateBanner
import com.botserver.mobile.ui.update.AppUpdateViewModel

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PairingScreen(
    autoPairRaw: String? = null,
    viewModel: PairingViewModel = hiltViewModel(),
    updateViewModel: AppUpdateViewModel = hiltViewModel(),
    onPaired: () -> Unit,
) {
    val context = LocalContext.current
    val state by viewModel.state.collectAsState()

    LaunchedEffect(autoPairRaw) {
        if (autoPairRaw != null) viewModel.onAutoPairLink(autoPairRaw)
    }

    // A self-update check needs no pairing at all — it talks to GitHub
    // directly (see AppUpdateViewModel/GitHubUpdateRepository) — so this
    // runs the moment the pairing screen appears, not gated behind ever
    // having paired. Auto-*checks* only; installing still needs one tap
    // (see AppUpdateBanner), never a surprise install prompt with no
    // action from the user.
    LaunchedEffect(Unit) { updateViewModel.checkForUpdate() }

    var hasCameraPermission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED,
        )
    }
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        hasCameraPermission = granted
    }

    var manualCode by remember { mutableStateOf("") }
    var showManualEntry by remember { mutableStateOf(false) }
    var showAdvancedManual by remember { mutableStateOf(false) }
    var advHost by remember { mutableStateOf("") }
    var advHost2 by remember { mutableStateOf("") }
    var advHost3 by remember { mutableStateOf("") }
    var advKey by remember { mutableStateOf("") }

    LaunchedEffect(state) {
        if (state is PairingState.Success) onPaired()
    }

    Scaffold(
        topBar = { TopAppBar(title = { Text("Pair with Bot Server") }) },
        // The Pair button lives in bottomBar, not as the scrollable
        // Column's last item, so it's always visible without scrolling
        // and gets real navigation-bar inset handling for free — the same
        // mechanism HomeScreen's own NavigationBar already relies on for
        // every other screen (this was the one screen without a bottomBar
        // at all). A plain trailing button at the end of a Column that
        // already fits the viewport stays at a fixed absolute position no
        // matter what padding is added to the Column, so pinning it here
        // instead is the more robust structural choice, not just cosmetic.
        bottomBar = {
            if (showManualEntry) {
                Surface(tonalElevation = 2.dp) {
                    Column(modifier = Modifier.navigationBarsPadding().padding(horizontal = 20.dp, vertical = 12.dp)) {
                        // Status feedback lives here, right next to the
                        // button — not only in the QR box above, which can
                        // be scrolled out of view while filling in the
                        // manual-entry fields below it. Tapping "Pair" with
                        // the fields scrolled into view previously gave no
                        // visible sign anything happened at all unless the
                        // user scrolled back up to see the QR box's spinner.
                        when (state) {
                            is PairingState.Verifying -> Row(verticalAlignment = Alignment.CenterVertically) {
                                CircularProgressIndicator(modifier = Modifier.size(16.dp), strokeWidth = 2.dp)
                                Spacer(Modifier.width(8.dp))
                                Text("Verifying…", style = MaterialTheme.typography.bodySmall, modifier = Modifier.testTag("pairing-status-verifying"))
                            }
                            is PairingState.Error -> Text(
                                (state as PairingState.Error).message,
                                color = MaterialTheme.colorScheme.error,
                                style = MaterialTheme.typography.bodySmall,
                                modifier = Modifier.testTag("pairing-status-error"),
                            )
                            else -> {}
                        }
                        if (state !is PairingState.Idle) Spacer(Modifier.height(8.dp))
                        Button(
                            onClick = {
                                if (showAdvancedManual) viewModel.onAdvancedManualSubmit(advHost, advKey, advHost2, advHost3)
                                else viewModel.onManualCodeSubmit(manualCode)
                            },
                            enabled = state !is PairingState.Verifying,
                            modifier = Modifier
                                .fillMaxWidth()
                                .testTag("pairing-submit"),
                        ) {
                            Text("Pair", fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(20.dp)
                .verticalScroll(rememberScrollState()),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            AppUpdateBanner(updateViewModel)
            Spacer(Modifier.height(12.dp))

            Text(
                "Scan the QR code from the dashboard's Mobile tab (Generate a key)",
                style = MaterialTheme.typography.bodyMedium,
                textAlign = androidx.compose.ui.text.style.TextAlign.Center,
            )
            Spacer(Modifier.height(16.dp))

            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .height(320.dp)
                    .clip(RoundedCornerShape(16.dp)),
                contentAlignment = Alignment.Center,
            ) {
                when {
                    state is PairingState.Verifying -> CircularProgressIndicator()
                    !hasCameraPermission -> Button(onClick = { permissionLauncher.launch(Manifest.permission.CAMERA) }) {
                        Text("Grant camera access")
                    }
                    else -> QrScannerView(onDetected = { raw -> viewModel.onScanned(raw) })
                }
            }

            if (state is PairingState.Error) {
                Spacer(Modifier.height(12.dp))
                Text(
                    (state as PairingState.Error).message,
                    color = MaterialTheme.colorScheme.error,
                    textAlign = androidx.compose.ui.text.style.TextAlign.Center,
                )
            }

            Spacer(Modifier.height(20.dp))
            TextButton(onClick = { showManualEntry = !showManualEntry }) {
                Text(if (showManualEntry) "Hide manual entry" else "Enter pairing code manually instead")
            }

            if (showManualEntry) {
                Column(modifier = Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                    if (!showAdvancedManual) {
                        Text(
                            "Paste the pairing code from the dashboard's Mobile tab or a Support Bot reply — " +
                                "it already carries every host this server is reachable at, nothing else to type.",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.7f),
                        )
                        Spacer(Modifier.height(8.dp))
                        OutlinedTextField(
                            value = manualCode,
                            onValueChange = { manualCode = it },
                            label = { Text("Pairing code") },
                            modifier = Modifier.fillMaxWidth().testTag("pairing-code"),
                            singleLine = false,
                            maxLines = 4,
                        )
                        Spacer(Modifier.height(8.dp))
                        TextButton(onClick = { showAdvancedManual = true }) {
                            Text("Paste isn't working? Enter host and key separately")
                        }
                    } else {
                        OutlinedTextField(
                            value = advHost,
                            onValueChange = { advHost = it },
                            label = { Text("Host:port (e.g. your-tailnet-host:8787)") },
                            modifier = Modifier.fillMaxWidth().testTag("pairing-host"),
                            singleLine = true,
                        )
                        Spacer(Modifier.height(8.dp))
                        OutlinedTextField(
                            value = advHost2,
                            onValueChange = { advHost2 = it },
                            label = { Text("Fallback host:port (optional)") },
                            modifier = Modifier.fillMaxWidth().testTag("pairing-host2"),
                            singleLine = true,
                        )
                        Spacer(Modifier.height(8.dp))
                        OutlinedTextField(
                            value = advHost3,
                            onValueChange = { advHost3 = it },
                            label = { Text("Public URL (optional, e.g. https://you.ts.net)") },
                            modifier = Modifier.fillMaxWidth().testTag("pairing-host3"),
                            singleLine = true,
                        )
                        Spacer(Modifier.height(8.dp))
                        OutlinedTextField(
                            value = advKey,
                            onValueChange = { advKey = it },
                            label = { Text("Key") },
                            modifier = Modifier.fillMaxWidth().testTag("pairing-key"),
                            singleLine = true,
                        )
                        Spacer(Modifier.height(8.dp))
                        TextButton(onClick = { showAdvancedManual = false }) {
                            Text("Back to pasting a pairing code")
                        }
                    }
                }
            }
        }
    }
}
