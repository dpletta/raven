using DocumentFormat.OpenXml;
using DocumentFormat.OpenXml.Packaging;
using DocumentFormat.OpenXml.Validation;

if (args.Length == 0)
{
    Console.Error.WriteLine("Usage: openxml-validator <docx path or directory> [...]");
    return 2;
}

var paths = args
    .SelectMany(argument =>
        Directory.Exists(argument)
            ? Directory.EnumerateFiles(argument, "*.docx", SearchOption.AllDirectories)
            : [argument])
    .Order(StringComparer.Ordinal)
    .ToArray();

if (paths.Length == 0)
{
    Console.Error.WriteLine("No DOCX files were found.");
    return 2;
}

var validator = new OpenXmlValidator(FileFormatVersions.Office2019);
var failures = 0;

foreach (var path in paths)
{
    try
    {
        using var document = WordprocessingDocument.Open(path, false);
        var errors = validator.Validate(document).ToArray();
        if (errors.Length == 0)
        {
            Console.WriteLine($"PASS {path}");
            continue;
        }

        failures++;
        Console.Error.WriteLine($"FAIL {path}: {errors.Length} Open XML validation error(s)");
        foreach (var error in errors.Take(100))
        {
            var part = error.Part?.Uri.ToString() ?? "<package>";
            var location = error.Path?.XPath ?? "<unknown>";
            Console.Error.WriteLine($"  {part} {location}: {error.Description}");
        }
    }
    catch (Exception error)
    {
        failures++;
        Console.Error.WriteLine($"FAIL {path}: {error.GetType().Name}: {error.Message}");
    }
}

return failures == 0 ? 0 : 1;
