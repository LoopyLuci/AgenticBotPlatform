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

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PairingScreen(
    autoPairRaw: String? = null,
    viewModel: PairingViewModel = hiltViewModel(),
    onPaired: () -> Unit,
) {
    val context = LocalContext.current
    val state by viewModel.state.collectAsState()

    LaunchedEffect(autoPairRaw) {
        if (autoPairRaw != null) viewModel.onAutoPairLink(autoPairRaw)
    }

    var hasCameraPermission by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED,
        )
    }
    val permissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        hasCameraPermission = granted
    }

    var manualHost by remember { mutableStateOf("") }
    var manualHost2 by remember { mutableStateOf("") }
    var manualHost3 by remember { mutableStateOf("") }
    var manualKey by remember { mutableStateOf("") }
    var showManualEntry by remember { mutableStateOf(false) }

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
                    Button(
                        onClick = { viewModel.onManualSubmit(manualHost, manualKey, manualHost2, manualHost3) },
                        enabled = state !is PairingState.Verifying,
                        modifier = Modifier
                            .fillMaxWidth()
                            .navigationBarsPadding()
                            .padding(20.dp)
                            .testTag("pairing-submit"),
                    ) {
                        Text("Pair", fontWeight = FontWeight.Bold)
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
                Text(if (showManualEntry) "Hide manual entry" else "Enter key manually instead")
            }

            if (showManualEntry) {
                Column(modifier = Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                    OutlinedTextField(
                        value = manualHost,
                        onValueChange = { manualHost = it },
                        label = { Text("Host:port (e.g. your-tailnet-host:8787)") },
                        modifier = Modifier.fillMaxWidth().testTag("pairing-host"),
                        singleLine = true,
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = manualHost2,
                        onValueChange = { manualHost2 = it },
                        label = { Text("Fallback host:port (optional)") },
                        modifier = Modifier.fillMaxWidth().testTag("pairing-host2"),
                        singleLine = true,
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = manualHost3,
                        onValueChange = { manualHost3 = it },
                        label = { Text("Public URL (optional, e.g. https://you.ts.net)") },
                        modifier = Modifier.fillMaxWidth().testTag("pairing-host3"),
                        singleLine = true,
                    )
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = manualKey,
                        onValueChange = { manualKey = it },
                        label = { Text("Key") },
                        modifier = Modifier.fillMaxWidth().testTag("pairing-key"),
                        singleLine = true,
                    )
                }
            }
        }
    }
}
