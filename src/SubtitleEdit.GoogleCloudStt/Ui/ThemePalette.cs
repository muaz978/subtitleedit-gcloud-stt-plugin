using Avalonia.Media;
using SubtitleEdit.GoogleCloudStt.Contract;

namespace SubtitleEdit.GoogleCloudStt.Ui;

/// <summary>
/// Subtitle Edit passes its active theme colors as #AARRGGBB strings so a plugin window
/// can match the host instead of looking like a foreign dialog. They may be absent on
/// older Subtitle Edit versions, so every colour has a fallback.
/// </summary>
public sealed class ThemePalette
{
    private ThemePalette(bool isDark, Color background, Color foreground, Color accent, Color panel, Color header)
    {
        IsDark = isDark;
        Background = new SolidColorBrush(background);
        Foreground = new SolidColorBrush(foreground);
        Accent = new SolidColorBrush(accent);
        Panel = new SolidColorBrush(panel);
        Header = new SolidColorBrush(header);
        Muted = new SolidColorBrush(foreground, 0.65);
        Line = new SolidColorBrush(foreground, 0.15);
    }

    public bool IsDark { get; }

    public IBrush Background { get; }

    public IBrush Foreground { get; }

    public IBrush Accent { get; }

    public IBrush Panel { get; }

    public IBrush Header { get; }

    public IBrush Muted { get; }

    public IBrush Line { get; }

    public static ThemePalette From(PluginThemeColors? colors)
    {
        var isDark = colors?.IsDark ?? true;

        var background = Parse(colors?.BackgroundColor, isDark ? Color.FromRgb(0x21, 0x21, 0x21) : Colors.White);
        var foreground = Parse(colors?.ForegroundColor, isDark ? Color.FromRgb(0xDC, 0xDC, 0xDC) : Color.FromRgb(0x1A, 0x1A, 0x1A));
        var accent = Parse(colors?.AccentColor, Color.FromRgb(0x1E, 0x90, 0xFF));
        var panel = Parse(colors?.BackgroundColorLighter, isDark ? Color.FromRgb(0x26, 0x26, 0x26) : Color.FromRgb(0xF5, 0xF5, 0xF5));
        var header = Parse(colors?.BackgroundColorHeader, isDark ? Color.FromRgb(0x30, 0x30, 0x30) : Color.FromRgb(0xEA, 0xEA, 0xEA));

        // The accent arrives with real alpha in some themes (#631E90FF), which looks washed
        // out on a button. Force it opaque and keep the hue.
        accent = Color.FromRgb(accent.R, accent.G, accent.B);

        return new ThemePalette(isDark, background, foreground, accent, panel, header);
    }

    private static Color Parse(string? value, Color fallback)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return fallback;
        }

        return Color.TryParse(value, out var parsed) ? parsed : fallback;
    }
}
