package np.gov.dhm.calibration.qrreader;

import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;
import androidx.appcompat.app.AppCompatActivity;
import com.google.zxing.integration.android.IntentIntegrator;
import com.google.zxing.integration.android.IntentResult;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import java.util.Iterator;

public class MainActivity extends AppCompatActivity {
    private EditText payloadInput;
    private TextView status;
    private TextView result;

    @Override protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        payloadInput = findViewById(R.id.payloadInput);
        status = findViewById(R.id.status);
        result = findViewById(R.id.result);
        ((Button)findViewById(R.id.scanButton)).setOnClickListener(v -> startScanner());
        ((Button)findViewById(R.id.readButton)).setOnClickListener(v -> readPayload(payloadInput.getText().toString().trim()));
        ((Button)findViewById(R.id.clearButton)).setOnClickListener(v -> {
            payloadInput.setText(""); result.setText(""); status.setText(getString(R.string.ready));
        });
    }

    private void startScanner() {
        IntentIntegrator integrator = new IntentIntegrator(this);
        integrator.setDesiredBarcodeFormats(IntentIntegrator.QR_CODE);
        integrator.setPrompt("Scan the DHM calibration certificate QR");
        integrator.setCameraId(0);
        integrator.setBeepEnabled(false);
        integrator.setBarcodeImageEnabled(false);
        integrator.setOrientationLocked(false);
        integrator.initiateScan();
    }

    @Override protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        IntentResult scan = IntentIntegrator.parseActivityResult(requestCode, resultCode, data);
        if (scan != null) {
            if (scan.getContents() == null) {
                status.setText("Scan cancelled.");
            } else {
                payloadInput.setText(scan.getContents());
                readPayload(scan.getContents());
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    private void readPayload(String raw) {
        final String prefix = "DHM-CAL-OFFLINE|";
        if (raw.isEmpty()) { status.setText("No QR payload supplied."); result.setText(""); return; }
        if (!raw.startsWith(prefix)) {
            status.setText("Not a DHM offline calibration QR payload.");
            result.setText("Expected prefix: " + prefix);
            return;
        }
        try {
            JSONObject json = new JSONObject(raw.substring(prefix.length()));
            if (!"DHM-CAL-OFFLINE".equals(json.optString("type")) || json.optInt("v", -1) != 1) {
                throw new JSONException("Unsupported DHM payload version/type.");
            }
            status.setText("✓ DHM calibration record loaded offline");
            result.setText(formatJson(json, 0));
        } catch (JSONException e) {
            status.setText("Invalid or incomplete calibration QR.");
            result.setText(e.getMessage() == null ? "Invalid JSON payload." : e.getMessage());
        }
    }

    private String formatJson(Object value, int indent) throws JSONException {
        StringBuilder out = new StringBuilder();
        String pad = " ".repeat(indent);
        if (value instanceof JSONObject) {
            JSONObject obj = (JSONObject)value;
            Iterator<String> keys = obj.keys();
            while (keys.hasNext()) {
                String key = keys.next(); Object child = obj.get(key);
                if (child instanceof JSONObject || child instanceof JSONArray) {
                    out.append(pad).append(label(key)).append(":\n");
                    out.append(formatJson(child, indent + 2));
                } else {
                    out.append(pad).append(label(key)).append(": ").append(String.valueOf(child)).append("\n");
                }
            }
        } else if (value instanceof JSONArray) {
            JSONArray array = (JSONArray)value;
            for (int i=0; i<array.length(); i++) {
                Object child = array.get(i);
                out.append(pad).append("• ");
                if (child instanceof JSONObject || child instanceof JSONArray) {
                    out.append("\n").append(formatJson(child, indent + 2));
                } else out.append(String.valueOf(child)).append("\n");
            }
        } else out.append(pad).append(String.valueOf(value)).append("\n");
        return out.toString();
    }

    private String label(String key) {
        StringBuilder s = new StringBuilder();
        for (char c : key.toCharArray()) s.append(c == '_' ? ' ' : c);
        if (s.length() > 0) s.setCharAt(0, Character.toUpperCase(s.charAt(0)));
        return s.toString();
    }
}
