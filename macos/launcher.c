// Friday.app's launcher.
//
// It runs Friday's own Python *inside this process* (instead of starting the python binary),
// so macOS sees "Friday": her name in the menu bar, her icon in the Dock, and permission
// prompts that say "Friday would like to access the microphone". Started the usual way,
// Homebrew's Python would relaunch itself as the generic "Python" app with a rocket icon.
//
// The project folder is read from Info.plist (FridayProjectPath); make-app.sh fills it in.

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <CoreFoundation/CoreFoundation.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void alert(const char *title, const char *message) {
    CFStringRef t = CFStringCreateWithCString(NULL, title, kCFStringEncodingUTF8);
    CFStringRef m = CFStringCreateWithCString(NULL, message, kCFStringEncodingUTF8);
    CFOptionFlags response = 0;
    CFUserNotificationDisplayAlert(0, kCFUserNotificationStopAlertLevel, NULL, NULL, NULL,
                                   t, m, CFSTR("OK"), NULL, NULL, &response);
    if (t) CFRelease(t);
    if (m) CFRelease(m);
}

static int project_path(char *out, size_t size) {
    CFBundleRef bundle = CFBundleGetMainBundle();
    if (!bundle) return 0;
    CFTypeRef value = CFBundleGetValueForInfoDictionaryKey(bundle, CFSTR("FridayProjectPath"));
    if (!value || CFGetTypeID(value) != CFStringGetTypeID()) return 0;
    return CFStringGetFileSystemRepresentation((CFStringRef)value, out, (CFIndex)size);
}

static int fail(const char *what, PyStatus status) {
    char msg[1024];
    snprintf(msg, sizeof msg, "%s: %s\n\nDetails are in ~/Library/Logs/Friday/friday.log.",
             what, status.err_msg ? status.err_msg : "unknown error");
    alert("Friday can't start", msg);
    return 1;
}

int main(int argc, char **argv) {
    char root[PATH_MAX], python[PATH_MAX], path_env[8192], msg[PATH_MAX + 400];
    struct stat st;

    if (!project_path(root, sizeof root)) {
        alert("Friday can't start", "This copy of Friday.app doesn't know where Friday's files are. "
              "Rebuild it by running ./make-app.sh in the friday folder.");
        return 1;
    }
    snprintf(python, sizeof python, "%s/.venv/bin/python", root);
    if (stat(python, &st) != 0) {
        snprintf(msg, sizeof msg, "Friday's files aren't where this app expects them:\n%s\n\n"
                 "If you moved the friday folder, run ./make-app.sh from its new location.", root);
        alert("Friday can't start", msg);
        return 1;
    }

    // Apps opened from the Dock get a bare PATH, but Friday uses Homebrew tools
    // (whisper-server for speech, node for the web-browser tool).
    const char *old_path = getenv("PATH");
    snprintf(path_env, sizeof path_env, "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:%s",
             (old_path && *old_path) ? old_path : "/usr/bin:/bin:/usr/sbin:/sbin");
    setenv("PATH", path_env, 1);
    setenv("FRIDAY_APP", "1", 1);  // tells Friday it's running as the Mac app
    setenv("PYTHONUTF8", "1", 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    if (!getenv("LANG")) setenv("LANG", "en_US.UTF-8", 1);
    unsetenv("PYTHONHOME");  // never pick up some other Python setup
    setenv("PYTHONPATH", root, 1);  // so `friday` (the package in this folder) can be imported
    if (chdir(root) != 0) {
        snprintf(msg, sizeof msg, "Couldn't open Friday's folder:\n%s", root);
        alert("Friday can't start", msg);
        return 1;
    }

    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 0;  // arguments go to Friday, not to Python

    // Pointing "executable" at the project's virtual environment makes Python use its packages.
    status = PyConfig_SetBytesString(&config, &config.executable, python);
    if (PyStatus_Exception(status)) goto error;
    status = PyConfig_SetBytesString(&config, &config.program_name, python);
    if (PyStatus_Exception(status)) goto error;
    status = PyConfig_SetString(&config, &config.run_module, L"friday");
    if (PyStatus_Exception(status)) goto error;

    // Pass options through (e.g. --no-voice when started from Terminal); drop the old -psn_ flag.
    char *args[64];
    int n = 0;
    args[n++] = python;
    for (int i = 1; i < argc && n < 63; i++) {
        if (strncmp(argv[i], "-psn_", 5) != 0) args[n++] = argv[i];
    }
    status = PyConfig_SetBytesArgv(&config, n, args);
    if (PyStatus_Exception(status)) goto error;

    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) goto error;
    PyConfig_Clear(&config);
    return Py_RunMain();

error:
    PyConfig_Clear(&config);
    return fail("Python couldn't start", status);
}
