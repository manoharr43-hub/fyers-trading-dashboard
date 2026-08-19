"""
File Scanner Module for NSE AI PRO
Manages, organizes, and analyzes saved scan reports
"""

import os
import shutil
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional
import json

class FileScanner:
    """Comprehensive file scanning and organization system."""
    
    def __init__(self, base_dir: str = "exports"):
        """Initialize file scanner with base directory."""
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
        # Create subdirectories
        self.subdirs = {
            "reports": self.base_dir / "reports",
            "fo_analysis": self.base_dir / "fo_analysis",
            "backups": self.base_dir / "backups",
            "archives": self.base_dir / "archives"
        }
        
        for subdir in self.subdirs.values():
            subdir.mkdir(parents=True, exist_ok=True)
    
    def scan_directory(self, include_subdirs: bool = True) -> List[Dict]:
        """
        Scan directory for scan files.
        
        Args:
            include_subdirs: Include subdirectories in scan
        
        Returns:
            List of file information dictionaries
        """
        files = []
        
        if include_subdirs:
            search_path = self.base_dir.rglob("*")
        else:
            search_path = self.base_dir.glob("*")
        
        for file_path in search_path:
            if file_path.is_file() and file_path.suffix in ['.xlsx', '.csv', '.json']:
                file_info = self._get_file_info(file_path)
                files.append(file_info)
        
        # Sort by modified time (newest first)
        return sorted(files, key=lambda x: x['modified_timestamp'], reverse=True)
    
    def _get_file_info(self, file_path: Path) -> Dict:
        """Extract detailed file information."""
        stat = file_path.stat()
        
        info = {
            "name": file_path.name,
            "path": str(file_path),
            "type": file_path.suffix[1:].upper(),
            "size_bytes": stat.st_size,
            "size_kb": round(stat.st_size / 1024, 2),
            "created": datetime.fromtimestamp(stat.st_ctime),
            "modified": datetime.fromtimestamp(stat.st_mtime),
            "created_str": datetime.fromtimestamp(stat.st_ctime).strftime("%d-%b-%Y %H:%M:%S"),
            "modified_str": datetime.fromtimestamp(stat.st_mtime).strftime("%d-%b-%Y %H:%M:%S"),
            "modified_timestamp": stat.st_mtime,
            "relative_path": file_path.relative_to(self.base_dir),
        }
        
        # Get file-specific metadata
        try:
            if file_path.suffix == '.csv':
                df = pd.read_csv(file_path)
                info["rows"] = len(df)
                info["columns"] = len(df.columns)
                info["column_names"] = df.columns.tolist()
            
            elif file_path.suffix == '.xlsx':
                excel_file = pd.ExcelFile(file_path)
                info["sheets"] = excel_file.sheet_names
                info["total_rows"] = sum(len(pd.read_excel(file_path, sheet)) for sheet in excel_file.sheet_names)
            
            elif file_path.suffix == '.json':
                with open(file_path, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        info["records"] = len(data)
                    elif isinstance(data, dict):
                        info["keys"] = list(data.keys())
        
        except Exception as e:
            info["error"] = str(e)
        
        return info
    
    def organize_by_date(self) -> Dict[str, List[Dict]]:
        """Organize files by creation date."""
        files = self.scan_directory()
        organized = {}
        
        for file_info in files:
            date_key = file_info['created'].strftime("%Y-%m-%d")
            if date_key not in organized:
                organized[date_key] = []
            organized[date_key].append(file_info)
        
        return organized
    
    def organize_by_type(self) -> Dict[str, List[Dict]]:
        """Organize files by type."""
        files = self.scan_directory()
        organized = {}
        
        for file_info in files:
            file_type = file_info['type']
            if file_type not in organized:
                organized[file_type] = []
            organized[file_type].append(file_info)
        
        return organized
    
    def get_statistics(self) -> Dict:
        """Get overall statistics about scanned files."""
        files = self.scan_directory()
        
        if not files:
            return {
                "total_files": 0,
                "total_size_mb": 0,
                "by_type": {},
                "oldest_file": None,
                "newest_file": None
            }
        
        stats = {
            "total_files": len(files),
            "total_size_bytes": sum(f['size_bytes'] for f in files),
            "total_size_mb": round(sum(f['size_bytes'] for f in files) / (1024 * 1024), 2),
            "by_type": {},
            "oldest_file": min(files, key=lambda x: x['modified_timestamp']),
            "newest_file": max(files, key=lambda x: x['modified_timestamp']),
            "avg_file_size_kb": round(sum(f['size_kb'] for f in files) / len(files), 2) if files else 0,
        }
        
        # Count by type
        for file_info in files:
            file_type = file_info['type']
            if file_type not in stats['by_type']:
                stats['by_type'][file_type] = {"count": 0, "size_mb": 0}
            stats['by_type'][file_type]['count'] += 1
            stats['by_type'][file_type]['size_mb'] += file_info['size_kb'] / 1024
            stats['by_type'][file_type]['size_mb'] = round(stats['by_type'][file_type]['size_mb'], 2)
        
        return stats
    
    def backup_file(self, file_name: str) -> bool:
        """Backup a specific file."""
        source = self.base_dir / file_name
        
        if not source.exists():
            print(f"❌ File not found: {file_name}")
            return False
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{source.stem}_backup_{timestamp}{source.suffix}"
        backup_path = self.subdirs['backups'] / backup_name
        
        try:
            shutil.copy2(source, backup_path)
            print(f"✅ Backup created: {backup_name}")
            return True
        except Exception as e:
            print(f"❌ Backup failed: {e}")
            return False
    
    def archive_old_files(self, days: int = 30) -> int:
        """Archive files older than specified days."""
        from datetime import timedelta
        
        files = self.scan_directory()
        cutoff_date = datetime.now() - timedelta(days=days)
        archived_count = 0
        
        for file_info in files:
            if file_info['modified'] < cutoff_date:
                source_path = Path(file_info['path'])
                archive_name = f"{file_info['name']}"
                archive_path = self.subdirs['archives'] / archive_name
                
                try:
                    shutil.move(str(source_path), str(archive_path))
                    archived_count += 1
                    print(f"📦 Archived: {file_info['name']}")
                except Exception as e:
                    print(f"❌ Failed to archive {file_info['name']}: {e}")
        
        return archived_count
    
    def cleanup_old_files(self, days: int = 90) -> int:
        """Delete files older than specified days."""
        from datetime import timedelta
        
        files = self.scan_directory()
        cutoff_date = datetime.now() - timedelta(days=days)
        deleted_count = 0
        
        for file_info in files:
            if file_info['modified'] < cutoff_date:
                try:
                    os.remove(file_info['path'])
                    deleted_count += 1
                    print(f"🗑️ Deleted: {file_info['name']}")
                except Exception as e:
                    print(f"❌ Failed to delete {file_info['name']}: {e}")
        
        return deleted_count
    
    def search_files(self, query: str, search_in: str = "name") -> List[Dict]:
        """
        Search files by name, type, or content.
        
        Args:
            query: Search query
            search_in: 'name', 'type', or 'all'
        
        Returns:
            List of matching files
        """
        files = self.scan_directory()
        query_lower = query.lower()
        results = []
        
        for file_info in files:
            if search_in in ['name', 'all']:
                if query_lower in file_info['name'].lower():
                    results.append(file_info)
                    continue
            
            if search_in in ['type', 'all']:
                if query_lower == file_info['type'].lower():
                    results.append(file_info)
        
        return results
    
    def export_manifest(self, output_file: str = None) -> str:
        """
        Export file manifest as JSON.
        
        Args:
            output_file: Output file path (default: manifest_TIMESTAMP.json)
        
        Returns:
            Path to exported manifest
        """
        if not output_file:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_file = self.base_dir / f"manifest_{timestamp}.json"
        
        files = self.scan_directory()
        manifest = {
            "generated_at": datetime.now().isoformat(),
            "total_files": len(files),
            "files": [
                {
                    "name": f['name'],
                    "type": f['type'],
                    "size_kb": f['size_kb'],
                    "modified": f['modified_str'],
                    "path": str(f['relative_path']),
                    **({"rows": f['rows'], "columns": f['columns']} if 'rows' in f else {}),
                }
                for f in files
            ]
        }
        
        with open(output_file, 'w') as f:
            json.dump(manifest, f, indent=2)
        
        print(f"✅ Manifest exported: {output_file}")
        return str(output_file)
    
    def get_report_summary(self, file_name: str) -> Dict:
        """Get summary of a specific report file."""
        files = self.scan_directory()
        file_info = next((f for f in files if f['name'] == file_name), None)
        
        if not file_info:
            return {"error": "File not found"}
        
        summary = {
            "file": file_info['name'],
            "type": file_info['type'],
            "size": f"{file_info['size_kb']} KB",
            "created": file_info['created_str'],
            "modified": file_info['modified_str'],
        }
        
        # Get data-specific summary
        try:
            if file_info['type'] == 'CSV' and 'rows' in file_info:
                summary["rows"] = file_info['rows']
                summary["columns"] = file_info['columns']
                summary["first_columns"] = file_info['column_names'][:5]
            
            elif file_info['type'] == 'XLSX':
                summary["sheets"] = file_info['sheets']
                summary["total_rows"] = file_info.get('total_rows', 0)
        
        except:
            pass
        
        return summary
    
    def print_summary(self) -> None:
        """Print formatted summary to console."""
        stats = self.get_statistics()
        
        print("\n" + "="*60)
        print("📊 FILE SCANNER SUMMARY")
        print("="*60)
        print(f"Total Files: {stats['total_files']}")
        print(f"Total Size: {stats['total_size_mb']} MB")
        print(f"Avg File Size: {stats['avg_file_size_kb']} KB")
        
        if stats['by_type']:
            print("\nBy Type:")
            for file_type, type_stats in stats['by_type'].items():
                print(f"  {file_type}: {type_stats['count']} files ({type_stats['size_mb']} MB)")
        
        if stats['newest_file']:
            print(f"\nNewest: {stats['newest_file']['name']} ({stats['newest_file']['modified_str']})")
        if stats['oldest_file']:
            print(f"Oldest: {stats['oldest_file']['name']} ({stats['oldest_file']['modified_str']})")
        
        print("="*60 + "\n")


# ════════════════════════════════════════════════════════════════════════════════
# COMMAND-LINE INTERFACE
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="NSE AI PRO File Scanner")
    parser.add_argument("--scan", action="store_true", help="Scan directory")
    parser.add_argument("--stats", action="store_true", help="Show statistics")
    parser.add_argument("--backup", type=str, help="Backup specific file")
    parser.add_argument("--archive", type=int, help="Archive files older than N days")
    parser.add_argument("--cleanup", type=int, help="Delete files older than N days")
    parser.add_argument("--search", type=str, help="Search for files")
    parser.add_argument("--manifest", action="store_true", help="Export manifest")
    parser.add_argument("--dir", type=str, default="exports", help="Base directory")
    
    args = parser.parse_args()
    
    scanner = FileScanner(args.dir)
    
    if args.scan:
        files = scanner.scan_directory()
        print(f"\n📂 Found {len(files)} files:\n")
        for f in files[:10]:
            print(f"  {f['name']} ({f['size_kb']} KB) - {f['modified_str']}")
        if len(files) > 10:
            print(f"  ... and {len(files) - 10} more")
    
    elif args.stats:
        scanner.print_summary()
    
    elif args.backup:
        scanner.backup_file(args.backup)
    
    elif args.archive:
        count = scanner.archive_old_files(args.archive)
        print(f"✅ Archived {count} files")
    
    elif args.cleanup:
        count = scanner.cleanup_old_files(args.cleanup)
        print(f"✅ Deleted {count} files")
    
    elif args.search:
        results = scanner.search_files(args.search)
        print(f"\n🔍 Found {len(results)} matching files:\n")
        for f in results:
            print(f"  {f['name']} - {f['modified_str']}")
    
    elif args.manifest:
        path = scanner.export_manifest()
        print(f"✅ Manifest saved to {path}")
    
    else:
        scanner.print_summary()
