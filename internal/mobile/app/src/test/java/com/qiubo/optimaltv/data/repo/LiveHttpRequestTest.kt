package com.qiubo.optimaltv.data.repo

import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import okhttp3.Call
import okhttp3.EventListener
import okhttp3.OkHttpClient
import okhttp3.Request
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveHttpRequestTest {
    @Test fun cancellingSourceLoadCancelsItsNetworkCall() = runBlocking {
        val started = CountDownLatch(1)
        val observed = AtomicReference<Call>()
        val client = OkHttpClient.Builder().eventListener(object : EventListener() {
            override fun callStart(call: Call) {
                observed.set(call)
                started.countDown()
            }
        }).build()
        ServerSocket(0).use { server ->
            val job = launch {
                liveHttpResponse(client, Request.Builder()
                    .url("http://127.0.0.1:${server.localPort}/slow-source").build()).close()
            }
            kotlinx.coroutines.yield()
            assertTrue(started.await(3, TimeUnit.SECONDS))
            job.cancelAndJoin()
            assertTrue(observed.get().isCanceled())
        }
        client.dispatcher.executorService.shutdownNow()
        client.connectionPool.evictAll()
    }
}
