package com.botserver.mobile.ui.nav

import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Chat
import androidx.compose.material.icons.filled.Devices
import androidx.compose.material.icons.filled.Dns
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.SmartToy
import androidx.compose.material.icons.filled.SupportAgent
import androidx.compose.material3.Icon
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.navigation.NavDestination.Companion.hierarchy
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.botserver.mobile.ui.bots.BotsScreen
import com.botserver.mobile.ui.chat.ChatScreen
import com.botserver.mobile.ui.devices.DevicesScreen
import com.botserver.mobile.ui.jobs.JobsScreen
import com.botserver.mobile.ui.providers.ProvidersScreen
import com.botserver.mobile.ui.serverchat.ServerChatScreen
import com.botserver.mobile.ui.sessions.SessionsScreen
import com.botserver.mobile.ui.settings.SettingsScreen
import com.botserver.mobile.ui.support.SupportBotScreen

private data class Tab(val route: String, val label: String, val icon: androidx.compose.ui.graphics.vector.ImageVector)

private val tabs = listOf(
    Tab("chat", "Chat", Icons.Filled.Chat),
    Tab("server-chat", "Server Chat", Icons.Filled.Dns),
    Tab("support", "Support", Icons.Filled.SupportAgent),
    Tab("sessions", "Sessions", Icons.Filled.Folder),
    Tab("jobs", "Jobs", Icons.Filled.List),
    Tab("bots", "Bots", Icons.Filled.SmartToy),
    Tab("settings", "Settings", Icons.Filled.Settings),
    Tab("devices", "Devices", Icons.Filled.Devices),
)

/** The paired-in-app shell — matching the dashboard's Chat / Server Chat /
 * Support Bot / Sessions / Jobs / Bots / Control Center tabs this Android
 * app mirrors, plus Devices for onboarding new devices from this one. */
@Composable
fun HomeScreen() {
    // Triggers HostSyncRepository.syncHosts() once per app session — see
    // HomeViewModel's doc. The return value is unused; this exists purely
    // for its init{} side effect, scoped to survive rotation.
    hiltViewModel<HomeViewModel>()
    val navController = rememberNavController()

    Scaffold(
        bottomBar = {
            NavigationBar {
                val backStackEntry by navController.currentBackStackEntryAsState()
                val currentRoute = backStackEntry?.destination
                tabs.forEach { tab ->
                    NavigationBarItem(
                        selected = currentRoute?.hierarchy?.any { it.route == tab.route } == true,
                        onClick = {
                            navController.navigate(tab.route) {
                                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = { Icon(tab.icon, contentDescription = tab.label) },
                        label = { Text(tab.label) },
                    )
                }
            }
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = "chat",
            modifier = Modifier.padding(padding),
        ) {
            composable("chat") { ChatScreen() }
            composable("server-chat") { ServerChatScreen() }
            composable("support") { SupportBotScreen() }
            composable("sessions") { SessionsScreen() }
            composable("jobs") { JobsScreen() }
            composable("bots") { BotsScreen() }
            composable("settings") { SettingsScreen(onOpenProviders = { navController.navigate("providers") }) }
            composable("providers") { ProvidersScreen(onBack = { navController.popBackStack() }) }
            composable("devices") { DevicesScreen() }
        }
    }
}
