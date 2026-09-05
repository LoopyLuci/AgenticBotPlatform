package com.botserver.mobile.ui.nav

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.botserver.mobile.data.HostSyncRepository
import dagger.hilt.android.lifecycle.HiltViewModel
import kotlinx.coroutines.launch
import javax.inject.Inject

/** Exists solely to trigger HostSyncRepository.syncHosts() once per app
 * session, right when the paired-in-app shell first appears — see that
 * repository's doc for why. A ViewModel (not a bare LaunchedEffect in
 * HomeScreen) so the sync survives configuration changes without
 * re-firing on every rotation. */
@HiltViewModel
class HomeViewModel @Inject constructor(
    private val hostSync: HostSyncRepository,
) : ViewModel() {
    init {
        viewModelScope.launch { hostSync.syncHosts() }
    }
}
