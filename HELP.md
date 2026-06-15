# MediaScout - Help Guide

## Table of Contents
- [Keyboard Shortcuts](#keyboard-shortcuts)
- [JDownloader Integration](#jdownloader-integration)
  - [Folder Watch Method](#folder-watch-method)
  - [Remote API Method](#remote-api-method-myjdownloader)
- [Troubleshooting](#troubleshooting)

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+S` | Start scan |
| `Escape` | Stop scan |
| `F5` | Refresh table |
| `Ctrl+,` | Open settings |
| `Ctrl+F` | Focus search box |
| `Ctrl+A` | Select/Deselect all items |
| `Ctrl+E` | Export results to CSV/JSON |

---

## JDownloader Integration

MediaScout can send downloads directly to JDownloader using two methods:

### Folder Watch Method

Best for: JDownloader running on the **same machine**.

#### Setup

1. **Find your JDownloader folderwatch path:**
   - Open JDownloader > Settings > Advanced Settings
   - Search for "folderwatch"
   - Copy the path from `org.jdownloader.container.FolderWatch: Folder`

   Default locations:
   - Windows: `C:\Users\YourName\JDownloader\folderwatch`
   - Mac/Linux: `~/JDownloader/folderwatch`

2. **Configure in MediaScout:**
   - Press `Ctrl+,` to open Settings
   - Go to **Automation** tab
   - Check **"Enable JDownloader (Watch Folder)"**
   - Enter or browse to your folderwatch path
   - Click **Save & Close**

3. **Verify:**
   - Status should show "Active"
   - Main button should show "SEND TO JDOWNLOADER" (green)

#### Usage

- **Single download:** Click RG or NF button on any item
- **Batch download:** Select multiple items, click "SEND TO JDOWNLOADER"
- Links appear as packages in JDownloader automatically

---

### Remote API Method (My.JDownloader)

Best for: JDownloader on a **different machine** or remote access.

#### Benefits
- Works over the internet
- No shared folders needed
- Control multiple JDownloader instances
- Cross-platform support

#### Setup

1. **Create My.JDownloader account:**
   - Go to https://my.jdownloader.org
   - Register and verify your email

2. **Connect JDownloader:**
   - Open JDownloader > Settings > My.JDownloader
   - Check "Enable My.JDownloader"
   - Enter email and password
   - Set a memorable **Device Name** (e.g., "Home PC")
   - Wait for status to show "Connected"

3. **Configure in MediaScout:**
   - Press `Ctrl+,` to open Settings
   - Go to **Automation** tab
   - Check "Enable JDownloader Integration"
   - Select **"Remote API (My.JDownloader)"**
   - Enter your email, password, and exact device name
   - Click **"Test API Connection"**
   - Click **Save & Close**

#### Using Environment Variables (Optional)

Create a `.env` file in the app folder:
```
JD_EMAIL=your-email@example.com
JD_PASSWORD=your_password
JD_DEVICE=Home PC
```

---

## Troubleshooting

### Folder Watch Issues

**Links not appearing in JDownloader?**
- Verify the folderwatch path matches exactly
- Ensure JDownloader is running
- Check that folderwatch is enabled in JDownloader Advanced Settings
- Restart JDownloader after enabling folderwatch

**Permission errors?**
- Ensure write access to the folderwatch folder
- Try running JDownloader as administrator

### Remote API Issues

**"Device not found"**
- Device name is case-sensitive - check it matches exactly
- Ensure JDownloader is running and connected (green status)
- Click "Test API Connection" to see available devices

**"Connection failed"**
- Verify email/password are correct
- Test login at https://my.jdownloader.org
- Check internet connection
- Install the API library: `pip install myjdapi`

**Links not appearing?**
- Check JDownloader's Linkgrabber tab (not Downloads)
- Links appear in Linkgrabber first, then move to Downloads
- Try manual test at my.jdownloader.org > Linkgrabber > Add Links

### General Tips

- Keep JDownloader running to process downloads
- Enable Debug Mode in Settings > Logs to see detailed messages
- If JDownloader fails, links are automatically copied to clipboard as fallback
- Each movie/show creates a separate, named package

---

## Which Method Should I Use?

| Scenario | Recommended Method |
|----------|-------------------|
| Same machine | Folder Watch |
| Different machine | Remote API |
| Remote/traveling | Remote API |
| No internet | Folder Watch |
| Multiple JDownloader instances | Remote API |
| Simplest setup | Folder Watch |

---

## Resources

- My.JDownloader: https://my.jdownloader.org
- JDownloader Forum: https://board.jdownloader.org
