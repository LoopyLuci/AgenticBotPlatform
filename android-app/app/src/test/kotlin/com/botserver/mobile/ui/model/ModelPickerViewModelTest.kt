package com.botserver.mobile.ui.model

import com.botserver.mobile.data.ApiService
import com.botserver.mobile.data.dto.ModelPickerProvider
import com.botserver.mobile.data.dto.ModelPickerResponse
import com.botserver.mobile.data.dto.SetModelResponse
import io.mockk.coEvery
import io.mockk.mockk
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/** ModelPickerViewModel — the native equivalent of Telegram's
 * interactive /model inline keyboard. ApiService is faked; the real
 * picker-data/set-model logic underneath has its own dedicated coverage
 * in test_model_picker_routes.py and test_hermes_model_discovery.py. */
class ModelPickerViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun buildViewModel(api: ApiService) = ModelPickerViewModel(api)

    @Test
    fun `open loads the provider list for a multi-provider backend`() = runTest {
        val response = ModelPickerResponse(
            mode = "providers",
            backend = "hermes_cli",
            providers = listOf(
                ModelPickerProvider(idx = 0, name = "openrouter", count = 2, freeCount = 1, isCurrent = false),
                ModelPickerProvider(idx = 1, name = "anthropic", count = 5, freeCount = 0, isCurrent = true),
            ),
            currentModel = "claude-sonnet-5",
        )
        val api = mockk<ApiService> { coEvery { modelPicker(17, null, 0) } returns response }
        val viewModel = buildViewModel(api)

        viewModel.open(17)
        assertTrue(viewModel.state.value.loading)
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertTrue(state.visible)
        assertFalse(state.loading)
        assertEquals("providers", state.mode)
        assertEquals(2, state.providers.size)
        assertEquals("claude-sonnet-5", state.currentModel)
    }

    @Test
    fun `selectProvider descends into that provider's model list`() = runTest {
        val providersResponse = ModelPickerResponse(mode = "providers", backend = "hermes_cli", providers = emptyList())
        val modelsResponse = ModelPickerResponse(
            mode = "models", backend = "hermes_cli", provider = "openrouter", providerIdx = 0,
            models = listOf("free-model", "paid-model"), page = 0, totalPages = 1, multiProvider = true,
        )
        val api = mockk<ApiService> {
            coEvery { modelPicker(17, null, 0) } returns providersResponse
            coEvery { modelPicker(17, 0, 0) } returns modelsResponse
        }
        val viewModel = buildViewModel(api)
        viewModel.open(17)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.selectProvider(0)
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertEquals("models", state.mode)
        assertEquals("openrouter", state.providerName)
        assertEquals(listOf("free-model", "paid-model"), state.models)
    }

    @Test
    fun `selectModel applies the change and closes the dialog on success`() = runTest {
        val response = ModelPickerResponse(mode = "models", backend = "api", models = listOf("m1"))
        val api = mockk<ApiService> {
            coEvery { modelPicker(1, null, 0) } returns response
            coEvery { setInstanceModel(1, any()) } returns SetModelResponse(ok = true, message = "Model set for this bot -> m1")
        }
        val viewModel = buildViewModel(api)
        viewModel.open(1)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.selectModel("m1")
        dispatcher.scheduler.advanceUntilIdle()

        assertFalse(viewModel.state.value.visible)
    }

    @Test
    fun `selectModel surfaces an error and keeps the dialog open on failure`() = runTest {
        val response = ModelPickerResponse(mode = "models", backend = "api", models = listOf("m1"))
        val api = mockk<ApiService> {
            coEvery { modelPicker(1, null, 0) } returns response
            coEvery { setInstanceModel(1, any()) } throws java.io.IOException("network down")
        }
        val viewModel = buildViewModel(api)
        viewModel.open(1)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.selectModel("m1")
        dispatcher.scheduler.advanceUntilIdle()

        val state = viewModel.state.value
        assertTrue(state.visible)
        assertEquals("network down", state.error)
    }

    @Test
    fun `dismiss resets to a closed state`() = runTest {
        val response = ModelPickerResponse(mode = "models", backend = "api", models = listOf("m1"))
        val api = mockk<ApiService> { coEvery { modelPicker(1, null, 0) } returns response }
        val viewModel = buildViewModel(api)
        viewModel.open(1)
        dispatcher.scheduler.advanceUntilIdle()

        viewModel.dismiss()

        assertFalse(viewModel.state.value.visible)
        assertNull(viewModel.state.value.instanceId)
    }
}
