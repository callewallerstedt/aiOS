package com.aios.mirror;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.DisplayCutout;
import android.view.RoundedCorner;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.webkit.WebChromeClient;
import android.webkit.PermissionRequest;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Landscape-only shell for the existing aiOS WebView UI served over USB-C. */
public final class MainActivity extends Activity {
    private static final int MICROPHONE_PERMISSION_REQUEST = 41;
    private static final String MIRROR_URL = "http://127.0.0.1:48738/mirror";
    private static final String HEALTH_URL = "http://127.0.0.1:48738/mirror-health";
    private static final long HEALTH_INTERVAL_MS = 4_000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService network = Executors.newSingleThreadExecutor();
    private WebView webView;
    private int healthMisses;
    private boolean serverOnline = true;
    private boolean stopped;
    private PermissionRequest pendingWebPermission;
    private int safeLeftCssPx;
    private int safeTopCssPx;
    private int safeRightCssPx;
    private int safeBottomCssPx;

    private final Runnable healthCheck = new Runnable() {
        @Override public void run() {
            if (stopped) return;
            network.execute(() -> {
                boolean healthy = isHealthy();
                handler.post(() -> applyHealth(healthy));
            });
        }
    };

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.BLACK);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }
        enterImmersiveMode();

        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(16, 23, 34));
        webView.setOnApplyWindowInsetsListener((view, insets) -> {
            updateSafeArea(insets);
            return insets;
        });
        configureWebView(webView);
        setContentView(webView);
        webView.requestApplyInsets();
        webView.loadUrl(MIRROR_URL);
        handler.post(healthCheck);
    }

    private void configureWebView(WebView view) {
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setUseWideViewPort(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setCacheMode(WebSettings.LOAD_NO_CACHE);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setUserAgentString(settings.getUserAgentString() + " aiOS-Mirror-Landscape/1.0");

        view.setWebChromeClient(new WebChromeClient() {
            @Override public void onPermissionRequest(PermissionRequest request) {
                runOnUiThread(() -> handleWebPermission(request));
            }
        });
        view.setWebViewClient(new WebViewClient() {
            @Override public void onPageFinished(WebView page, String url) {
                super.onPageFinished(page, url);
                applySafeAreaToPage();
                if (url != null && url.startsWith("http://127.0.0.1:48738/")) applyHealth(true);
            }

            @Override public void onReceivedError(
                    WebView page, WebResourceRequest request, WebResourceError error) {
                super.onReceivedError(page, request, error);
                if (request.isForMainFrame()) applyHealth(false);
            }

            @Override public boolean shouldOverrideUrlLoading(
                    WebView page, WebResourceRequest request) {
                return openExternalIfNeeded(request.getUrl());
            }

            @SuppressWarnings("deprecation")
            @Override public boolean shouldOverrideUrlLoading(WebView page, String url) {
                return openExternalIfNeeded(Uri.parse(url));
            }
        });
    }

    private void updateSafeArea(WindowInsets insets) {
        int left = 0;
        int top = 0;
        int right = 0;
        int bottom = 0;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            DisplayCutout cutout = insets.getDisplayCutout();
            if (cutout != null) {
                left = cutout.getSafeInsetLeft();
                top = cutout.getSafeInsetTop();
                right = cutout.getSafeInsetRight();
                bottom = cutout.getSafeInsetBottom();
            }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            RoundedCorner topLeft = insets.getRoundedCorner(RoundedCorner.POSITION_TOP_LEFT);
            RoundedCorner bottomLeft = insets.getRoundedCorner(RoundedCorner.POSITION_BOTTOM_LEFT);
            RoundedCorner topRight = insets.getRoundedCorner(RoundedCorner.POSITION_TOP_RIGHT);
            RoundedCorner bottomRight = insets.getRoundedCorner(RoundedCorner.POSITION_BOTTOM_RIGHT);
            left = Math.max(left, Math.max(cornerRadius(topLeft), cornerRadius(bottomLeft)));
            right = Math.max(right, Math.max(cornerRadius(topRight), cornerRadius(bottomRight)));
        }

        float density = getResources().getDisplayMetrics().density;
        safeLeftCssPx = physicalToCssPx(left, density);
        safeTopCssPx = physicalToCssPx(top, density);
        safeRightCssPx = physicalToCssPx(right, density);
        safeBottomCssPx = physicalToCssPx(bottom, density);
        applySafeAreaToPage();
    }

    private static int cornerRadius(RoundedCorner corner) {
        return corner == null ? 0 : corner.getRadius();
    }

    private static int physicalToCssPx(int pixels, float density) {
        if (pixels <= 0) return 0;
        return (int) Math.ceil(pixels / Math.max(1f, density)) + 4;
    }

    private void applySafeAreaToPage() {
        if (webView == null) return;
        String script = "document.documentElement.style.setProperty('--phone-safe-left','"
                + safeLeftCssPx + "px');"
                + "document.documentElement.style.setProperty('--phone-safe-top','"
                + safeTopCssPx + "px');"
                + "document.documentElement.style.setProperty('--phone-safe-right','"
                + safeRightCssPx + "px');"
                + "document.documentElement.style.setProperty('--phone-safe-bottom','"
                + safeBottomCssPx + "px');";
        webView.evaluateJavascript(script, null);
    }

    private void handleWebPermission(PermissionRequest request) {
        boolean wantsAudio = false;
        for (String resource : request.getResources()) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) wantsAudio = true;
        }
        if (!wantsAudio) {
            request.deny();
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            return;
        }
        pendingWebPermission = request;
        requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, MICROPHONE_PERMISSION_REQUEST);
    }

    @Override public void onRequestPermissionsResult(
            int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != MICROPHONE_PERMISSION_REQUEST || pendingWebPermission == null) return;
        if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            pendingWebPermission.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
        } else {
            pendingWebPermission.deny();
        }
        pendingWebPermission = null;
    }

    private boolean openExternalIfNeeded(Uri uri) {
        if (uri != null && "127.0.0.1".equals(uri.getHost())) return false;
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, uri));
        } catch (Exception ignored) {
        }
        return true;
    }

    private boolean isHealthy() {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(HEALTH_URL).openConnection();
            connection.setConnectTimeout(1_500);
            connection.setReadTimeout(1_500);
            connection.setUseCaches(false);
            return connection.getResponseCode() == 200;
        } catch (Exception ignored) {
            return false;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private void applyHealth(boolean healthy) {
        if (stopped) return;
        if (healthy) {
            healthMisses = 0;
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (!serverOnline) {
                serverOnline = true;
                webView.loadUrl(MIRROR_URL);
            }
        } else if (++healthMisses >= 3) {
            serverOnline = false;
            getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (healthMisses >= 6) finishAndRemoveTask();
        }
        handler.removeCallbacks(healthCheck);
        handler.postDelayed(healthCheck, HEALTH_INTERVAL_MS);
    }

    private void enterImmersiveMode() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
    }

    @Override protected void onResume() {
        super.onResume();
        enterImmersiveMode();
        if (!stopped) handler.post(healthCheck);
    }

    @Override public void onBackPressed() {
        if (webView != null && webView.canGoBack()) webView.goBack();
        else moveTaskToBack(true);
    }

    @Override protected void onDestroy() {
        stopped = true;
        handler.removeCallbacksAndMessages(null);
        network.shutdownNow();
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
        }
        super.onDestroy();
    }
}
