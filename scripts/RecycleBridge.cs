using System;
using System.IO;
using System.Runtime.InteropServices;

namespace CodexSafeRecycle {
  [ComImport,Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IShellItem {
    [PreserveSig]int BindToHandler(IntPtr a,ref Guid b,ref Guid c,out IntPtr d);
    [PreserveSig]int GetParent(out IShellItem parent);
    [PreserveSig]int GetDisplayName(uint sigdn,out IntPtr name);
    [PreserveSig]int GetAttributes(uint mask,out uint attributes);
    [PreserveSig]int Compare(IShellItem other,uint hint,out int order);
  }
  [ComImport,Guid("947AAB5F-0A5C-4C13-B4D6-4BF7836FC9F8"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IFileOperation {
    [PreserveSig]int Advise(IFileOperationProgressSink sink,out uint cookie);
    [PreserveSig]int Unadvise(uint cookie);
    [PreserveSig]int SetOperationFlags(uint flags);
    [PreserveSig]int SetProgressMessage([MarshalAs(UnmanagedType.LPWStr)]string message);
    [PreserveSig]int SetProgressDialog(IntPtr dialog);
    [PreserveSig]int SetProperties(IntPtr properties);
    [PreserveSig]int SetOwnerWindow(IntPtr window);
    [PreserveSig]int ApplyPropertiesToItem(IShellItem item);
    [PreserveSig]int ApplyPropertiesToItems(IntPtr items);
    [PreserveSig]int RenameItem(IShellItem item,[MarshalAs(UnmanagedType.LPWStr)]string name,IFileOperationProgressSink sink);
    [PreserveSig]int RenameItems(IntPtr items,[MarshalAs(UnmanagedType.LPWStr)]string name);
    [PreserveSig]int MoveItem(IShellItem item,IShellItem destination,[MarshalAs(UnmanagedType.LPWStr)]string name,IFileOperationProgressSink sink);
    [PreserveSig]int MoveItems(IntPtr items,IShellItem destination);
    [PreserveSig]int CopyItem(IShellItem item,IShellItem destination,[MarshalAs(UnmanagedType.LPWStr)]string name,IFileOperationProgressSink sink);
    [PreserveSig]int CopyItems(IntPtr items,IShellItem destination);
    [PreserveSig]int DeleteItem(IShellItem item,IFileOperationProgressSink sink);
    [PreserveSig]int DeleteItems(IntPtr items);
    [PreserveSig]int NewItem(IShellItem folder,uint attributes,[MarshalAs(UnmanagedType.LPWStr)]string name,[MarshalAs(UnmanagedType.LPWStr)]string template,IFileOperationProgressSink sink);
    [PreserveSig]int PerformOperations();
    [PreserveSig]int GetAnyOperationsAborted([MarshalAs(UnmanagedType.Bool)]out bool aborted);
  }
  [ComVisible(true),Guid("04B0F1A7-9490-44BC-96E1-4296A31252E2"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IFileOperationProgressSink {
    [PreserveSig]int StartOperations();
    [PreserveSig]int FinishOperations(int hr);
    [PreserveSig]int PreRenameItem(uint f,IShellItem item,[MarshalAs(UnmanagedType.LPWStr)]string n);
    [PreserveSig]int PostRenameItem(uint f,IShellItem item,[MarshalAs(UnmanagedType.LPWStr)]string n,int hr,IShellItem created);
    [PreserveSig]int PreMoveItem(uint f,IShellItem item,IShellItem dst,[MarshalAs(UnmanagedType.LPWStr)]string n);
    [PreserveSig]int PostMoveItem(uint f,IShellItem item,IShellItem dst,[MarshalAs(UnmanagedType.LPWStr)]string n,int hr,IShellItem created);
    [PreserveSig]int PreCopyItem(uint f,IShellItem item,IShellItem dst,[MarshalAs(UnmanagedType.LPWStr)]string n);
    [PreserveSig]int PostCopyItem(uint f,IShellItem item,IShellItem dst,[MarshalAs(UnmanagedType.LPWStr)]string n,int hr,IShellItem created);
    [PreserveSig]int PreDeleteItem(uint f,IShellItem item);
    [PreserveSig]int PostDeleteItem(uint f,IShellItem item,int hr,IShellItem created);
    [PreserveSig]int PreNewItem(uint f,IShellItem dst,[MarshalAs(UnmanagedType.LPWStr)]string n);
    [PreserveSig]int PostNewItem(uint f,IShellItem dst,[MarshalAs(UnmanagedType.LPWStr)]string n,[MarshalAs(UnmanagedType.LPWStr)]string template,uint attributes,int hr,IShellItem created);
    [PreserveSig]int UpdateProgress(uint total,uint done);
    [PreserveSig]int ResetTimer();
    [PreserveSig]int PauseTimer();
    [PreserveSig]int ResumeTimer();
  }
  public class Result {
    public bool ok=false;
    public bool aborted=false;
    public int hresult=0;
    public uint pre_flags=0;
    public string recycle_path="";
    public string recycle_parsing="";
    public string destination_path="";
    public string error="";
  }
  [ComVisible(true),ClassInterface(ClassInterfaceType.None)]
  public sealed class Sink:IFileOperationProgressSink {
    public string Mode,Source,Destination;
    public ulong ExpectedId;
    public readonly Result Result=new Result();
    const int ABORT=unchecked((int)0x80004004);
    public int StartOperations(){return 0;}
    public int FinishOperations(int hr){Result.hresult=hr;return 0;}
    public int PreDeleteItem(uint flags,IShellItem item){
      Result.pre_flags=flags;
      // Refuse permanent deletes, unexpected roots and a replaced directory identity.
      if(Mode!="recycle"||(flags&0x80)==0||!Operation.SamePath(Operation.Name(item,0x80058000),Source)||Operation.FileId(Source)!=ExpectedId){
        Result.error="Native recycle guard rejected this operation";return ABORT;
      }
      return 0;
    }
    public int PostDeleteItem(uint flags,IShellItem item,int hr,IShellItem created){
      Result.hresult=hr;
      if(hr>=0&&created!=null){
        Result.recycle_path=Operation.Name(created,0x80058000);
        Result.recycle_parsing=Operation.Name(created,0x80028000);
        Result.ok=Result.recycle_path.IndexOf("\\$Recycle.Bin\\",StringComparison.OrdinalIgnoreCase)>=0;
        string receipt=Environment.GetEnvironmentVariable("CODEX_RECYCLE_RECEIPT");
        if(Result.ok&&!String.IsNullOrEmpty(receipt)){
          // Persist the Shell callback before returning to the Python coordinator.
          byte[] bytes=System.Text.Encoding.UTF8.GetBytes(Result.recycle_path+"\n"+Result.recycle_parsing+"\n");
          using(var stream=new FileStream(receipt,FileMode.CreateNew,FileAccess.Write,FileShare.Read,4096,FileOptions.WriteThrough)){
            stream.Write(bytes,0,bytes.Length);stream.Flush(true);
          }
        }
      }
      return 0;
    }
    public int PreMoveItem(uint f,IShellItem item,IShellItem dst,string n){
      if(Mode!="restore"||File.Exists(Destination)||Directory.Exists(Destination)||(f&2)!=0||Operation.FileId(Operation.Name(item,0x80058000))!=ExpectedId){Result.error="Restore target exists, source changed or overwrite requested";return ABORT;}
      return 0;
    }
    public int PostMoveItem(uint f,IShellItem item,IShellItem dst,string n,int hr,IShellItem created){
      Result.hresult=hr;
      if(hr>=0&&created!=null){Result.destination_path=Operation.Name(created,0x80058000);Result.ok=Operation.SamePath(Result.destination_path,Destination);}
      return 0;
    }
    public int PreRenameItem(uint f,IShellItem item,string n){return ABORT;}
    public int PostRenameItem(uint f,IShellItem item,string n,int hr,IShellItem created){return 0;}
    public int PreCopyItem(uint f,IShellItem item,IShellItem dst,string n){return ABORT;}
    public int PostCopyItem(uint f,IShellItem item,IShellItem dst,string n,int hr,IShellItem created){return 0;}
    public int PreNewItem(uint f,IShellItem dst,string n){return ABORT;}
    public int PostNewItem(uint f,IShellItem dst,string n,string t,uint a,int hr,IShellItem created){return 0;}
    public int UpdateProgress(uint a,uint b){return 0;}
    public int ResetTimer(){return 0;}
    public int PauseTimer(){return 0;}
    public int ResumeTimer(){return 0;}
  }
  public static class Operation {
    [DllImport("shell32.dll",CharSet=CharSet.Unicode,PreserveSig=true)]
    static extern int SHCreateItemFromParsingName(string path,IntPtr bind,ref Guid iid,out IShellItem item);
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    static extern IntPtr CreateFile(string path,uint access,uint share,IntPtr security,uint creation,uint flags,IntPtr template);
    [DllImport("kernel32.dll",SetLastError=true)]static extern bool GetFileInformationByHandle(IntPtr h,out FileInfo info);
    [DllImport("kernel32.dll")]static extern bool CloseHandle(IntPtr h);
    [StructLayout(LayoutKind.Sequential)]struct FileInfo {
      public uint attributes,createdLow,createdHigh,accessLow,accessHigh,writeLow,writeHigh,volume,sizeHigh,sizeLow,links,indexHigh,indexLow;
    }
    public static ulong FileId(string path){
      var h=CreateFile(path,0,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero);
      if(h==new IntPtr(-1))throw new IOException("Cannot inspect source identity");
      try{FileInfo i;if(!GetFileInformationByHandle(h,out i))throw new IOException("Cannot read source identity");return ((ulong)i.indexHigh<<32)|i.indexLow;}finally{CloseHandle(h);}
    }
    public static bool SamePath(string a,string b){return String.Equals(a.TrimEnd('\\'),b.TrimEnd('\\'),StringComparison.OrdinalIgnoreCase);}
    public static string Name(IShellItem item,uint type){
      IntPtr p=IntPtr.Zero;try{if(item.GetDisplayName(type,out p)<0)return "";return Marshal.PtrToStringUni(p)??"";}finally{if(p!=IntPtr.Zero)Marshal.FreeCoTaskMem(p);}
    }
    static IShellItem Item(string path){Guid iid=typeof(IShellItem).GUID;IShellItem x;Marshal.ThrowExceptionForHR(SHCreateItemFromParsingName(path,IntPtr.Zero,ref iid,out x));return x;}
    public static Result Run(string mode,string path,string destination,string expectedId){
      var sink=new Sink{Mode=mode,Source=path,Destination=destination,ExpectedId=UInt64.Parse(expectedId)};
      IFileOperation op=null;IShellItem src=null,dst=null;
      try{
        if(System.Threading.Thread.CurrentThread.GetApartmentState()!=System.Threading.ApartmentState.STA)throw new InvalidOperationException("STA required");
        if(mode!="recycle"&&mode!="restore")throw new InvalidOperationException("Unsupported operation");
        op=(IFileOperation)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("3AD05575-8857-4850-9277-11B85BDB8E09")));
        // No permanent-delete API or fallback. WANTNUKEWARNING overrides automatic Yes.
        uint flags=0x0004|0x0010|0x0040|0x0400|0x2000|0x4000|0x00100000|0x00800000|0x20000000;
        if(mode=="recycle")flags|=0x00080000;else flags|=0x0008;
        Marshal.ThrowExceptionForHR(op.SetOperationFlags(flags));
        src=Item(path);
        if(mode=="recycle")Marshal.ThrowExceptionForHR(op.DeleteItem(src,sink));
        else{
          if(File.Exists(destination)||Directory.Exists(destination))throw new IOException("Restore target exists");
          dst=Item(Path.GetDirectoryName(destination));
          Marshal.ThrowExceptionForHR(op.MoveItem(src,dst,Path.GetFileName(destination),sink));
        }
        int hr=op.PerformOperations();bool aborted;op.GetAnyOperationsAborted(out aborted);
        sink.Result.aborted=aborted;
        if(hr<0)sink.Result.hresult=hr;
        if(aborted||hr<0)sink.Result.ok=false;
      }catch(Exception e){sink.Result.ok=false;sink.Result.error=e.Message;sink.Result.hresult=Marshal.GetHRForException(e);}
      finally{
        if(dst!=null)Marshal.FinalReleaseComObject(dst);
        if(src!=null)Marshal.FinalReleaseComObject(src);
        if(op!=null)Marshal.FinalReleaseComObject(op);
        GC.KeepAlive(sink);
      }
      return sink.Result;
    }
  }
}
