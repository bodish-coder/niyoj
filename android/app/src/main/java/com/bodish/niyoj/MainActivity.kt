package com.bodish.niyoj

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebView
import androidx.appcompat.app.AppCompatActivity

/** The whole app: the desktop ui.html in a WebView, with Bridge standing in for
 *  pywebview's js_api. No second UI to keep in step with the desktop one. */
class MainActivity : AppCompatActivity() {

    lateinit var web: WebView
        private set

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            addJavascriptInterface(Bridge(this@MainActivity), "NiYoj")
        }
        setContentView(web)
        val html = assets.open("ui.html").bufferedReader().use { it.readText() }
        // a named base URL, not about:blank, so the page has a stable origin;
        // nothing is ever fetched from it — the html is passed in whole
        web.loadDataWithBaseURL("https://niyoj.local/", html, "text/html", "utf-8", null)
    }

    /** Run JS on the UI thread. Kotlin's half of the pywebview evaluate_js bridge. */
    fun js(code: String) {
        web.post { web.evaluateJavascript(code, null) }
    }

    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }
}
