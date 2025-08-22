#!/usr/bin/env python3

import argparse
import getpass
import json
import os
import re
import time
import unicodedata

import requests

CONFIG_PATH = os.path.expanduser("~/.config/onshape-exporter.json")
BASE_URL = "https://cad.onshape.com"

HEADERS = {
    "Accept": "application/vnd.onshape.v1+json",
    "Content-Type": "application/json",
}


def verbose_request(method, url, **kwargs):
    verbose = kwargs.pop("verbose", False)
    if verbose:
        print(f"\n[{method}] {url}")
        if "headers" in kwargs:
            print(" Headers:", json.dumps(kwargs["headers"], indent=2))
        if "json" in kwargs:
            print(" JSON:", json.dumps(kwargs["json"], indent=2))
        if "data" in kwargs:
            print(" Data:", kwargs["data"])
        if "params" in kwargs:
            print(" Params:", kwargs["params"])
    r = requests.request(method, url, **kwargs)
    if verbose:
        print(f" [HTTP {r.status_code}] -> {len(r.content)} bytes received")
    return r

def slugify(value, allow_unicode=False):
    value = str(value)
    value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    )
    value = re.sub(r"[^\w\s-]", "", value.lower())
    return re.sub(r"[-\s]+", "-", value).strip("-_")


def load_credentials():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
            return config["access_key"], config["secret_key"]
    else:
        access_key = input("Enter Onshape Access Key: ")
        secret_key = getpass.getpass("Enter Onshape Secret Key: ")
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump({"access_key": access_key, "secret_key": secret_key}, f)
        return access_key, secret_key


ACCESS_KEY, SECRET_KEY = load_credentials()


def parse_url(url):
    match = re.search(r"/documents/([^/]+)/[wv]/([^/]+)/e/([^/?#]+)", url)
    if not match:
        raise ValueError("Invalid Onshape URL format")
    return match.groups()  # (did, wid_or_vid, eid)


def get_part_studio_name(did, wid, eid, verbose=False):
    """Get the name of the part studio element.

    Tries multiple API endpoints to retrieve the part studio name.
    """
    # Try using the document elements endpoint
    path = f"/api/v6/documents/d/{did}/w/{wid}/elements"
    url = f"{BASE_URL}{path}"

    headers = HEADERS.copy()

    try:
        r = verbose_request(
            "GET", url, headers=headers, auth=(ACCESS_KEY, SECRET_KEY), verbose=verbose
        )
        if r.status_code == 200:
            elements = r.json()
            for element in elements:
                if element.get("id") == eid:
                    return element.get("name", "part")
    except Exception as e:
        print(f"⚠️ First attempt to get part studio name failed: {e}")

    # Try the part studio metadata endpoint
    try:
        path = f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/metadata"
        url = f"{BASE_URL}{path}"

        r = verbose_request(
            "GET", url, headers=headers, auth=(ACCESS_KEY, SECRET_KEY), verbose=verbose
        )
        if r.status_code == 200:
            metadata = r.json()
            return metadata.get("name", "part")
    except Exception as e:
        print(f"⚠️ Second attempt to get part studio name failed: {e}")

    # Try the configuration API as it might contain element metadata
    try:
        path = f"/api/v6/elements/d/{did}/w/{wid}/e/{eid}/configuration"
        url = f"{BASE_URL}{path}"

        r = verbose_request(
            "GET", url, headers=headers, auth=(ACCESS_KEY, SECRET_KEY), verbose=verbose
        )
        if r.status_code == 200:
            config_data = r.json()
            if "elementName" in config_data:
                return config_data.get("elementName")
    except Exception:
        pass

    print("⚠️ Could not retrieve part studio name, using default")
    return "part"


def get_configurations(did, wid, eid, verbose=False):
    """Get configurations from a part studio.

    This function:
    1. Gets the configuration data using getConfiguration API
    2. Encodes each configuration option using encodeConfigurationMap API
    3. Returns a list of configuration objects with query parameters and display names
    """
    # First, get the configuration data
    path = f"/api/v6/elements/d/{did}/w/{wid}/e/{eid}/configuration"
    url = f"{BASE_URL}{path}"

    headers = HEADERS.copy()

    r = verbose_request(
        "GET", url, headers=headers, auth=(ACCESS_KEY, SECRET_KEY), verbose=verbose
    )
    if r.status_code != 200:
        print(f"Failed to get configurations: {r.status_code} {r.text}")
        return []

    config_data = r.json()
    # print(json.dumps(config_data, indent=2))

    # Extract all configuration parameters
    config_params = config_data.get("configurationParameters", [])
    if not config_params:
        # No configurations found
        return [{"configurationParametersQuery": "", "configurationDisplay": "Default"}]

    result = []

    # Add the default configuration
    result.append(
        {"configurationParametersQuery": "", "configurationDisplay": "Default"}
    )

    # Check if there's only one configuration parameter for simplified naming
    num_params = len(config_params)

    # Process each configuration parameter
    for config_param in config_params:
        param_id = config_param.get("parameterId")
        param_name = config_param.get("parameterName")
        options = config_param.get("options", [])

        for option in options:
            option_value = option.get("option")
            option_name = option.get("optionName")

            # Skip the default option since we've already added it
            # Create parameter map for encoding
            param_map = {
                "parameters": [
                    {"parameterId": param_id, "parameterValue": option_value}
                ]
            }

            # Encode the configuration
            encode_path = f"/api/v6/elements/d/{did}/e/{eid}/configurationencodings"
            encode_url = f"{BASE_URL}{encode_path}"

            r = verbose_request(
                "POST",
                encode_url,
                headers=headers,
                json=param_map,
                auth=(ACCESS_KEY, SECRET_KEY),
                verbose=verbose,
            )
            if r.status_code != 200:
                print(
                    f"Failed to encode configuration {option_name}: {r.status_code} {r.text}"
                )
                continue

            encoding_data = r.json()
            query_param = encoding_data.get("queryParam", "")

            # Add to results - simplify name if there's only one parameter
            if num_params == 1:
                display_name = option_name
            else:
                display_name = f"{param_name} - {option_name}"

            result.append(
                {
                    "configurationParametersQuery": query_param,
                    "configurationDisplay": display_name,
                }
            )

    return result


def export_stl_sync(
    did,
    wid,
    eid,
    config_query_str,
    config_display_name,
    output_dir,
    part_studio_name=None,
    resolution=None,
    verbose=False,
):
    format_upper = "STL"

    configuration = None
    if config_query_str:
        config_match = re.search(r"configuration=([^&]+)", config_query_str)
        if config_match:
            configuration = config_match.group(1)

    print(
        f"Exporting {format_upper} (sync) for {part_studio_name} {config_display_name}",
        end="",
        flush=True,
    )

    path = f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/stl"
    url = f"{BASE_URL}{path}"

    params = {}
    res = (resolution or "fine").lower()
    if res not in ("coarse", "medium", "fine", "veryfine"):
        print(f"Warning: Invalid STL resolution '{res}', defaulting to 'fine'")
        res = "fine"

    # presets = {
    #     "coarse": {
    #         "angleTolerance": 12.5,
    #         "chordTolerance": 0.00024,
    #         "minFacetWidth": 0.000635,
    #     },
    #     "medium": {
    #         "angleTolerance": 6.25,
    #         "chordTolerance": 0.00012,
    #         "minFacetWidth": 0.000254,
    #     },
    #     "fine": {
    #         "angleTolerance": 2.5,
    #         "chordTolerance": 0.00006,
    #         "minFacetWidth": 0.0000254,
    #     },
    # }

    # Note: angleTolerance was problematic in testing, causing 400 errors
    presets = {
        "coarse": {
            "chordTolerance": 0.00024,
            "minFacetWidth": 0.000635,
        },
        "medium": {
            "chordTolerance": 0.00012,
            "minFacetWidth": 0.000254,
        },
        "fine": {
            "chordTolerance": 0.00006,
            "minFacetWidth": 0.0000254,
        },
    }
    params.update(presets[res])

    if configuration:
        try:
            import urllib.parse
            decoded_config = urllib.parse.unquote(configuration)
            params["configuration"] = decoded_config
        except Exception:
            params["configuration"] = configuration

    # First request, expect 307 redirect
    r = verbose_request(
        "GET",
        url,
        headers={"Accept": "application/octet-stream"},
        params=params,
        auth=(ACCESS_KEY, SECRET_KEY),
        allow_redirects=False,
        verbose=verbose,
    )

    if r.status_code in (302, 303, 307, 308) and "Location" in r.headers:
        redirect_url = r.headers["Location"]
        r = verbose_request(
            "GET",
            redirect_url,
            headers={"Accept": "application/octet-stream"},
            auth=(ACCESS_KEY, SECRET_KEY),
            verbose=verbose,
        )

    if r.status_code != 200:
        print(
            f"\n❌ Failed STL sync export: {r.status_code} {r.text[:100]}..."
            if hasattr(r, "text") and len(r.text) > 100
            else f"\n❌ Failed STL sync export: {r.status_code}"
        )
        return

    # Save the file using the same naming convention
    names = []
    if part_studio_name:
        names.append(part_studio_name)
        if config_display_name and config_display_name != "Default":
            names.append(config_display_name)
    else:
        names.append(config_display_name)

    name = "-".join(slugify(n) for n in names)
    filename = os.path.join(output_dir, f"{name}.stl")

    with open(filename, "wb") as f:
        f.write(r.content)
    print(f"\n✅ Saved {filename}")


def export_file(
    did,
    wid,
    eid,
    config_query_str,
    config_display_name,
    format_,
    output_dir,
    part_studio_name=None,
    resolution=None,
    verbose=False,
):
    """Export a file using the asynchronous export API.

    This approach works with all formats including STEP, IGES, STL, etc.
    """
    format_upper = format_.upper()

    # Extract configuration from query string if provided
    configuration = None
    if config_query_str:
        config_match = re.search(r"configuration=([^&]+)", config_query_str)
        if config_match:
            configuration = config_match.group(1)

    print(
        f"Exporting {format_upper} for {part_studio_name} {config_display_name}",
        end="",
        flush=True,
    )

    # 1. Create the translation job
    path = f"/api/v6/partstudios/d/{did}/w/{wid}/e/{eid}/translations"
    url = f"{BASE_URL}{path}"

    headers = HEADERS.copy()
    headers["Accept"] = "application/json;charset=UTF-8; qs=0.09"
    headers["Content-Type"] = "application/json;charset=UTF-8; qs=0.09"

    # Prepare minimal request body with only required parameters
    body = {
        "formatName": format_upper,
        "storeInDocument": False,
    }

    if format_upper == "STL":
        res = (resolution or "fine").lower()
        if res not in ("coarse", "medium", "fine", "veryfine"):
            print(f"Warning: Invalid STL resolution '{res}', defaulting to 'fine'")
            res = "fine"
        body["resolution"] = res

    # Add configuration if provided
    if configuration:
        # Per Onshape API docs, configuration needs to be formatted differently
        # for the asynchronous translation API
        try:
            import urllib.parse

            decoded_config = urllib.parse.unquote(configuration)

            # Extract parameter ID and value from the decoded config string
            # Format typically looks like: "List_abc123=_value"
            param_parts = decoded_config.split("=")
            if len(param_parts) == 2:
                param_id = param_parts[0]
                param_value = param_parts[1]

                # For the translation API, configuration should be a simple string
                # just use the decoded parameter directly
                body["configuration"] = param_id + "=" + param_value
            else:
                # If we can't parse correctly, pass as-is
                body["configuration"] = decoded_config
                print(f"Warning: Configuration format unexpected: {decoded_config}")
        except Exception as e:
            # If processing fails, use as-is
            body["configuration"] = configuration
            print(f"Warning: Could not process configuration parameter: {e}")

    # Create the translation job
    try:
        r = verbose_request(
            "POST",
            url,
            json=body,
            headers=headers,
            auth=(ACCESS_KEY, SECRET_KEY),
            verbose=verbose,
        )

        if r.status_code != 200:
            print(
                f"❌ Failed to create translation job: {r.status_code} {r.text[:100]}..."
                if len(r.text) > 100
                else f"❌ Failed to create translation job: {r.status_code} {r.text}"
            )
            return
    except Exception as e:
        print(f"❌ Exception during translation job creation: {e}")
        return

    # Get translation job details
    translation_data = r.json()
    translation_id = translation_data.get("id")
    request_state = translation_data.get("requestState")

    # 2. Poll until the job is complete
    max_attempts = 30
    attempts = 0

    while request_state == "ACTIVE" and attempts < max_attempts:
        print(".", end="", flush=True)
        time.sleep(2)  # Wait 2 seconds between polls
        attempts += 1

        # Check translation status
        status_url = f"{BASE_URL}/api/v6/translations/{translation_id}"
        r = requests.get(
            status_url,
            headers={"Accept": "application/json;charset=UTF-8; qs=0.09"},
            auth=(ACCESS_KEY, SECRET_KEY),
        )

        if r.status_code != 200:
            print(f"\n❌ Failed to check translation status: {r.status_code}")
            return

        translation_data = r.json()
        request_state = translation_data.get("requestState")

    # Add newline after progress dots
    print()

    # 3. Check if the translation completed successfully
    if request_state != "DONE":
        if request_state == "FAILED":
            failure_reason = translation_data.get("failureReason", "Unknown failure")
            print(f"❌ Translation failed: {failure_reason}")
        else:
            print(f"❌ Translation timed out or had unexpected state: {request_state}")
        return

    # 4. Download the result
    result_ids = translation_data.get("resultExternalDataIds", [])

    if not result_ids:
        print("❌ No result files available for download")
        return

    # Download each result file
    for i, result_id in enumerate(result_ids):
        download_url = f"{BASE_URL}/api/v6/documents/d/{did}/externaldata/{result_id}"

        print(f"Downloading...", end="", flush=True)

        r = requests.get(
            download_url,
            headers={"Accept": "application/octet-stream"},
            auth=(ACCESS_KEY, SECRET_KEY),
        )

        if r.status_code != 200:
            print(f"\r❌ Failed to download result: {r.status_code}")
            continue

        print("\r", end="")  # Clear the downloading message

        # Save the file using part studio name if available
        names = []
        if part_studio_name:
            # Clean up the part studio name for use in filenames
            names.append(part_studio_name)

            # Clean up configuration name
            if config_display_name and config_display_name != "Default":
                names.append(config_display_name)

        else:
            # Fall back to old naming scheme if part studio name not available
            names.append(config_display_name)

        name = "-".join(slugify(n) for n in names)
        file_index = f"_{i+1}" if len(result_ids) > 1 else ""

        filename = os.path.join(
            output_dir, f"{name}{file_index}.{format_upper.lower()}"
        )

        with open(filename, "wb") as f:
            f.write(r.content)
        print(f"✅ Saved {filename}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export Onshape part configurations.")
    parser.add_argument("url", help="Onshape document URL")
    parser.add_argument("output_dir", help="Directory to save exported files")
    parser.add_argument(
        "-f",
        "--format",
        dest="formats",
        action="append",
        required=True,
        help="Export format (e.g., STL, STEP). Can be specified multiple times.",
    )
    parser.add_argument(
        "-c",
        "--config",
        dest="configs",
        action="append",
        default=[],
        help="Configuration parameter override in the form parameterId=value. Can be used multiple times.",
    )
    parser.add_argument(
        "--resolution",
        dest="resolution",
        choices=["coarse", "medium", "fine"],
        help="STL mesh resolution. Only applies to STL exports.",
    )
    parser.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        help="Print verbose API call information.",
    )
    args = parser.parse_args()

    if not args.formats:
        parser.error("At least one -f/--format must be specified")

    os.makedirs(args.output_dir, exist_ok=True)
    did, wid, eid = parse_url(args.url)

    # Get the part studio name to use in filenames
    part_studio_name = get_part_studio_name(did, wid, eid, verbose=args.verbose)
    # print(f"Part Studio: {part_studio_name}")

    # If specific configs are provided via -c, encode and export only those
    if args.configs:
        # Build parameters list for encoding API
        parameters = []
        for kv in args.configs:
            if "=" not in kv:
                print(f"Ignoring invalid -c value: {kv} (expected parameterId=value)")
                continue
            pid, val = kv.split("=", 1)
            # Convert common literals
            if val.lower() in ("true", "false"):
                param_value = True if val.lower() == "true" else False
            else:
                try:
                    if "." in val:
                        param_value = float(val)
                    else:
                        param_value = int(val)
                except ValueError:
                    param_value = val
            parameters.append({"parameterId": pid, "parameterValue": param_value})

        if not parameters:
            print("No valid -c configurations provided; falling back to discovered configurations")
        else:
            encode_path = f"/api/v6/elements/d/{did}/e/{eid}/configurationencodings"
            encode_url = f"{BASE_URL}{encode_path}"
            headers = HEADERS.copy()
            try:
                r = verbose_request(
                    "POST",
                    encode_url,
                    headers=headers,
                    json={"parameters": parameters},
                    auth=(ACCESS_KEY, SECRET_KEY),
                    verbose=args.verbose,
                )
                if r.status_code != 200:
                    print(f"Failed to encode provided configurations: {r.status_code} {r.text}")
                    parameters = []
                else:
                    enc = r.json()
                    query_str = enc.get("queryParam", "")
                    # Display name based on provided overrides
                    disp_parts = []
                    for p in parameters:
                        v = p["parameterValue"]
                        if isinstance(v, bool):
                            v_str = "true" if v else "false"
                        else:
                            v_str = str(v)
                        disp_parts.append(f"{p['parameterId']}={v_str}")
                    display_name = ", ".join(disp_parts) if disp_parts else "Custom"
                    for fmt in args.formats:
                        if fmt.upper() == "STL":
                            export_stl_sync(
                                did=did,
                                wid=wid,
                                eid=eid,
                                config_query_str=query_str,
                                config_display_name=display_name,
                                output_dir=args.output_dir,
                                part_studio_name=part_studio_name,
                                resolution=args.resolution,
                                verbose=args.verbose,
                            )
                        else:
                            export_file(
                                did=did,
                                wid=wid,
                                eid=eid,
                                config_query_str=query_str,
                                config_display_name=display_name,
                                format_=fmt,
                                output_dir=args.output_dir,
                                part_studio_name=part_studio_name,
                                resolution=args.resolution,
                                verbose=args.verbose,
                            )
                    # Done, exit main
                    exit(0)
            except Exception as e:
                print(f"Error encoding provided configurations: {e}")
                parameters = []

    # Fall back to discovered configurations
    configs = get_configurations(did, wid, eid, verbose=args.verbose)
    for config in configs:
        query_str = config.get("configurationParametersQuery")
        display_name = config.get("configurationDisplay")
        if not display_name:
            continue
        for fmt in args.formats:
            if fmt.upper() == "STL":
                export_stl_sync(
                    did=did,
                    wid=wid,
                    eid=eid,
                    config_query_str=query_str,
                    config_display_name=display_name,
                    output_dir=args.output_dir,
                    part_studio_name=part_studio_name,
                    resolution=args.resolution,
                    verbose=args.verbose,
                )
            else:
                export_file(
                    did=did,
                    wid=wid,
                    eid=eid,
                    config_query_str=query_str,
                    config_display_name=display_name,
                    format_=fmt,
                    output_dir=args.output_dir,
                    part_studio_name=part_studio_name,
                    resolution=args.resolution,
                    verbose=args.verbose,
                )
