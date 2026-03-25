interface Props {
  message?: string;
}

export default function EmptyState({ message = "No data available yet." }: Props) {
  return (
    <div className="card flex items-center justify-center py-16 text-gray-500 text-sm">
      {message}
    </div>
  );
}
